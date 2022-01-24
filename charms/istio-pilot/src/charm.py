#!/usr/bin/env python3

import logging
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase, RelationBrokenEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces
from charms.istio_pilot.v0.ingress_per_unit import IngressPerUnitProvider


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return

        self.ingress_per_unit = IngressPerUnitProvider(self)

        try:
            self.interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            self.model.unit.status = WaitingStatus(str(err))
            return
        except NoCompatibleVersions as err:
            self.model.unit.status = BlockedStatus(str(err))
            return
        else:
            self.model.unit.status = ActiveStatus()

        self.log = logging.getLogger(__name__)

        self.env = Environment(loader=FileSystemLoader('src'))

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.remove, self.remove)

        self.framework.observe(self.on.config_changed, self.handle_default_gateways)

        self.framework.observe(self.on["istio-pilot"].relation_changed, self.send_info)

        self.framework.observe(self.on['ingress'].relation_changed, self.handle_ingress)
        self.framework.observe(self.on['ingress'].relation_departed, self.handle_ingress)
        self.framework.observe(self.on['ingress'].relation_broken, self.handle_ingress)
        self.framework.observe(self.on['ingress-auth'].relation_changed, self.handle_ingress_auth)
        self.framework.observe(self.on['ingress-auth'].relation_departed, self.handle_ingress_auth)

        self.framework.observe(self.ingress_per_unit.on.ready, self.handle_ingress_per_unit)
        self.framework.observe(self.ingress_per_unit.on.failed, self.handle_ingress_per_unit_failed)

    def install(self, event):
        """Install charm."""

        subprocess.check_call(
            [
                "./istioctl",
                "install",
                "-y",
                "-s",
                "profile=minimal",
                "-s",
                f"values.global.istioNamespace={self.model.name}",
            ]
        )

        self.unit.status = ActiveStatus()

    def remove(self, event):
        """Remove charm."""

        manifests = subprocess.check_output(
            [
                "./istioctl",
                "manifest",
                "generate",
                "-s",
                "profile=minimal",
                "-s",
                f"values.global.istioNamespace={self.model.name}",
            ]
        )

        try:
            self._kubectl(
                "delete",
                "virtualservices,destinationrule,gateways,envoyfilters,rbacconfigs",
                f"-lapp.juju.is/created-by={self.app.name}",
                capture_output=True,
            )
            self._kubectl(
                'delete',
                "--ignore-not-found",
                "-f-",
                input=manifests,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            if b"(Unauthorized)" in e.stderr:
                # Ignore error from https://bugs.launchpad.net/juju/+bug/1941655
                self.log.error(f"Ignoring unauthorized error during cleanup:\n{e.stderr}")
            else:
                self.log.error(e.stderr)
                raise

    def handle_default_gateways(self, event):
        t = self.env.get_template('gateway.yaml.j2')
        gateways = self.model.config['default-gateways'].split(',')
        manifest = ''.join(t.render(name=g) for g in gateways)
        self._kubectl(
            'delete',
            'gateways',
            f'-lapp.juju.is/created-by={self.app.name}',
        )
        self._kubectl("apply", "-f-", input=manifest)

    def send_info(self, event):
        if self.interfaces["istio-pilot"]:
            self.interfaces["istio-pilot"].send_data(
                {
                    "service-name": f'istiod.{self.model.name}.svc',
                    "service-port": '15012',
                }
            )

    def handle_ingress(self, event):
        gateway_address = self._get_gateway_address()
        if not gateway_address:
            self.unit.status = WaitingStatus("Waiting for gateway address")
            event.defer()
            return
        else:
            self.unit.status = ActiveStatus()

        ingress = self.interfaces['ingress']
        if ingress:
            # Filter out data we sent back.
            routes = {
                (rel, app): route
                for (rel, app), route in sorted(
                    ingress.get_data().items(), key=lambda tup: tup[0][0].id
                )
                if app != self.app
            }
        else:
            routes = {}

        if isinstance(event, RelationBrokenEvent):
            # The app-level data is still visible on a broken relation, but we
            # shouldn't be keeping the VirtualService for that related app.
            del routes[(event.relation, event.app)]

        t = self.env.get_template('virtual_service.yaml.j2')
        gateway = self.model.config['default-gateways'].split(',')[0]

        def get_kwargs(rel, version, route):
            """Handles both v1 and v2 ingress relations.

            v1 ingress schema doesn't allow sending over a namespace.
            """
            kwargs = {'gateway': gateway, **route}

            if 'namespace' not in kwargs:
                kwargs['namespace'] = self.model.name

            prefix = kwargs["prefix"]
            kwargs.setdefault("rewrite", prefix)

            return kwargs

        virtual_services = ''.join(
            t.render(**get_kwargs(rel, ingress.versions[app.name], route))
            for ((rel, app), route) in routes.items()
        )

        self._kubectl(
            'delete',
            'virtualservices,destinationrules',
            f'-lapp.juju.is/created-by={self.app.name}',
        )
        if routes:
            self._kubectl("apply", "-f-", input=virtual_services)

    def handle_ingress_auth(self, event):
        auth_routes = self.interfaces['ingress-auth']
        if auth_routes:
            auth_routes = list(auth_routes.get_data().values())
        else:
            auth_routes = []

        if not all(ar.get("service") for ar in auth_routes):
            self.model.unit.status = WaitingStatus("Waiting for auth route connection information.")
            return

        rbac_configs = Path('src/rbac_config.yaml').read_text() if auth_routes else None

        t = self.env.get_template('auth_filter.yaml.j2')
        auth_filters = ''.join(
            t.render(
                namespace=self.model.name,
                **{
                    'request_headers': yaml.safe_dump(
                        [{'exact': h} for h in r.get('allowed-request-headers', [])],
                        default_flow_style=True,
                    ),
                    'response_headers': yaml.safe_dump(
                        [{'exact': h} for h in r.get('allowed-response-headers', [])],
                        default_flow_style=True,
                    ),
                    'port': r['port'],
                    'service': r['service'],
                },
            )
            for r in auth_routes
        )

        manifests = [rbac_configs, auth_filters]
        manifests = '\n'.join([m for m in manifests if m])
        self._kubectl(
            'delete',
            'envoyfilters,rbacconfigs',
            f'-lapp.juju.is/created-by={self.app.name}',
        )

        self._kubectl("apply", "-f-", input=manifests)

    def handle_ingress_per_unit(self, event):
        gateway_address = self._get_gateway_address()
        if not gateway_address:
            self.unit.status = WaitingStatus("Waiting for gateway address")
            event.defer()
            return
        else:
            self.unit.status = ActiveStatus()

        request = self.ingress_per_unit.get_request(event.relation)
        self._kubectl(
            'delete',
            'virtualservices,destinationrules',
            '-l',
            ','.join(
                [
                    f'app.juju.is/created-by={self.app.name}',
                    f'app.juju.is/for={request.name}',
                ]
            ),
        )
        t = self.env.get_template('virtual_service.yaml.j2')
        self._kubectl("apply", "-f-", input=t.render(gateway=gateway_address, request=request))

        request.send_urls(
            {unit: f"http://{gateway_address}/{unit.prefix}/" for unit in request.units}
        )

    def handle_ingress_per_unit_broken(self, event):
        request = self.ingress_per_unit.get_request(event.relation)
        try:
            self._kubectl(
                'delete',
                'virtualservices,destinationrules',
                '-l',
                ','.join(
                    [
                        f'app.juju.is/created-by={self.app.name}',
                        f'app.juju.is/for={request.name}',
                    ]
                ),
            )
        except subprocess.CalledProcessError as e:
            self.log.exception(
                f"Failed to clean up for {event.relation}: {e.stderr or e.stdout}"
            )

    def handle_ingress_per_unit_failed(self, event):
        self.charm.unit.status = self.ingress_per_unit.get_status(event.relation)

    def _kubectl(self, *args, namespace=None, input=None, capture_output=False):
        """Helper for running kubectl."""
        if isinstance(input, str):
            input = input.encode("utf-8")
        res = subprocess.run(
            [
                './kubectl',
                '-n',
                namespace or self.model.name,
                *args,
            ],
            input=input,
            capture_output=capture_output,
            check=True,
        )
        if capture_output:
            return res.stdout.decode("utf-8")

    def _get_gateway_address(self):
        """Determine the external address for the ingress gateway.

        It will prefer the `external-hostname` config if that is set, otherwise
        it will look up the load balancer address for the ingress gateway.

        If the gateway isn't available or doesn't have a load balancer address yet,
        returns None.
        """
        if self.model.config["external-hostname"]:
            return self.model.config["external-hostname"]
        svcs = yaml.safe_load(
            self._kubectl(
                "get",
                "svc",
                "-l",
                "istio=ingressgateway",
                "-oyaml",
                namespace=self.model.name,
                capture_output=True,
            )
        )
        if not svcs["items"]:
            return None
        addrs = svcs["items"][0]["status"].get("loadBalancer", {}).get("ingress", [])
        if not addrs:
            return None
        return addrs[0].get("hostname", addrs[0].get("ip"))


if __name__ == "__main__":
    main(Operator)
