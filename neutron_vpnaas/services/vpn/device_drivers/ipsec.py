# Copyright 2013, Nachi Ueno, NTT I3, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import abc
import copy
import os
import re
import shutil
import six
import socket

import jinja2
import netaddr
from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.api.v2 import attributes
from neutron.common import rpc as n_rpc
from neutron.common import utils as n_utils
from neutron import context
from neutron.i18n import _LE
from neutron.plugins.common import constants
from neutron.plugins.common import utils as plugin_utils
from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging
from oslo_service import loopingcall
from neutron.agent.l3.dvr_local_router import DvrLocalRouter

from neutron_vpnaas.extensions import vpnaas
from neutron_vpnaas.services.vpn.common import topics
from neutron_vpnaas.services.vpn import device_drivers

LOG = logging.getLogger(__name__)
TEMPLATE_PATH = os.path.dirname(os.path.abspath(__file__))
DVR_VPN_IP_RULE_PRIORITY = 32768

ipsec_opts = [
    cfg.StrOpt(
        'config_base_dir',
        default='$state_path/ipsec',
        help=_('Location to store ipsec server config files')),
    cfg.IntOpt('ipsec_status_check_interval',
               default=60,
               help=_("Interval for checking ipsec status")),
    cfg.BoolOpt('enable_detailed_logging',
                default=False,
                help=_("Enable detail logging for ipsec pluto process. "
                       "If the flag set to True, the detailed logging will "
                       "be written into config_base_dir/<pid>/log.")),
]
cfg.CONF.register_opts(ipsec_opts, 'ipsec')

openswan_opts = [
    cfg.StrOpt(
        'ipsec_config_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/openswan/ipsec.conf.template'),
        help=_('Template file for ipsec configuration')),
    cfg.StrOpt(
        'ipsec_secret_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/openswan/ipsec.secret.template'),
        help=_('Template file for ipsec secret configuration'))
]

cfg.CONF.register_opts(openswan_opts, 'openswan')

JINJA_ENV = None

IPSEC_CONNS = 'ipsec_site_connections'


def _get_template(template_file):
    global JINJA_ENV
    if not JINJA_ENV:
        templateLoader = jinja2.FileSystemLoader(searchpath="/")
        JINJA_ENV = jinja2.Environment(loader=templateLoader)
    return JINJA_ENV.get_template(template_file)


@six.add_metaclass(abc.ABCMeta)
class BaseSwanProcess(object):
    """Swan Family Process Manager

    This class manages start/restart/stop ipsec process.
    This class create/delete config template
    """

    binary = "ipsec"
    CONFIG_DIRS = [
        'var/run',
        'log',
        'etc',
        'etc/ipsec.d/aacerts',
        'etc/ipsec.d/acerts',
        'etc/ipsec.d/cacerts',
        'etc/ipsec.d/certs',
        'etc/ipsec.d/crls',
        'etc/ipsec.d/ocspcerts',
        'etc/ipsec.d/policies',
        'etc/ipsec.d/private',
        'etc/ipsec.d/reqs',
        'etc/pki/nssdb/'
    ]

    DIALECT_MAP = {
        "3des": "3des",
        "aes-128": "aes128",
        "aes-256": "aes256",
        "aes-192": "aes192",
        "group2": "modp1024",
        "group5": "modp1536",
        "group14": "modp2048",
        "group15": "modp3072",
        "bi-directional": "start",
        "response-only": "add",
        "v2": "insist",
        "v1": "never"
    }

    STATUS_DICT = {
        'erouted': constants.ACTIVE,
        'unrouted': constants.DOWN
    }
    STATUS_RE = '\d\d\d "([a-f0-9\-]+).* (unrouted|erouted);'
    STATUS_NOT_RUNNING_RE = 'Command:.*ipsec.*status.*Exit code: [1|3]$'

    def __init__(self, conf, process_id, vpnservice, namespace):
        self.conf = conf
        self.id = process_id
        self.updated_pending_status = False
        self.namespace = namespace
        self.connection_status = {}
        self.config_dir = os.path.join(
            self.conf.ipsec.config_base_dir, self.id)
        self.etc_dir = os.path.join(self.config_dir, 'etc')
        self.log_dir = os.path.join(self.config_dir, 'log')
        self.update_vpnservice(vpnservice)
        self.STATUS_PATTERN = re.compile(self.STATUS_RE)
        self.STATUS_NOT_RUNNING_PATTERN = re.compile(
            self.STATUS_NOT_RUNNING_RE)
        self.STATUS_MAP = self.STATUS_DICT

    def translate_dialect(self):
        if not self.vpnservice:
            return
        for ipsec_site_conn in self.vpnservice['ipsec_site_connections']:
            self._dialect(ipsec_site_conn, 'initiator')
            self._dialect(ipsec_site_conn['ikepolicy'], 'ike_version')
            for key in ['encryption_algorithm',
                        'auth_algorithm',
                        'pfs']:
                self._dialect(ipsec_site_conn['ikepolicy'], key)
                self._dialect(ipsec_site_conn['ipsecpolicy'], key)

    def update_vpnservice(self, vpnservice):
        self.vpnservice = vpnservice
        self.translate_dialect()

    def _dialect(self, obj, key):
        obj[key] = self.DIALECT_MAP.get(obj[key], obj[key])

    @abc.abstractmethod
    def ensure_configs(self):
        pass

    def ensure_config_file(self, kind, template, vpnservice, file_mode=None):
        """Update config file,  based on current settings for service."""
        config_str = self._gen_config_content(template, vpnservice)
        config_file_name = self._get_config_filename(kind)
        if file_mode is None:
            utils.replace_file(config_file_name, config_str)
        else:
            utils.replace_file(config_file_name, config_str, file_mode)

    def remove_config(self):
        """Remove whole config file."""
        shutil.rmtree(self.config_dir, ignore_errors=True)

    def _get_config_filename(self, kind):
        config_dir = self.etc_dir
        return os.path.join(config_dir, kind)

    def ensure_config_dir(self, vpnservice):
        """Create config directory if it does not exist."""
        n_utils.ensure_dir(self.config_dir)
        for subdir in self.CONFIG_DIRS:
            dir_path = os.path.join(self.config_dir, subdir)
            n_utils.ensure_dir(dir_path)

    def _gen_config_content(self, template_file, vpnservice):
        template = _get_template(template_file)
        return template.render(
            {'vpnservice': vpnservice,
             'state_path': self.conf.state_path})

    @abc.abstractmethod
    def get_status(self):
        pass

    @property
    def status(self):
        if self.active:
            return constants.ACTIVE
        return constants.DOWN

    @property
    def active(self):
        """Check if the process is active or not."""
        if not self.namespace:
            return False
        try:
            status = self.get_status()
            self._extract_and_record_connection_status(status)
            if not self.connection_status:
                return False
        except RuntimeError:
            return False
        return True

    def update(self):
        """Update Status based on vpnservice configuration."""
        if self.vpnservice and not self.vpnservice['admin_state_up']:
            self.disable()
        else:
            self.enable()

        if plugin_utils.in_pending_status(self.vpnservice['status']):
            self.updated_pending_status = True

        self.vpnservice['status'] = self.status
        for ipsec_site_conn in self.vpnservice['ipsec_site_connections']:
            if plugin_utils.in_pending_status(ipsec_site_conn['status']):
                conn_id = ipsec_site_conn['id']
                conn_status = self.connection_status.get(conn_id)
                if not conn_status:
                    continue
                conn_status['updated_pending_status'] = True
                ipsec_site_conn['status'] = conn_status['status']

    def enable(self):
        """Enabling the process."""
        try:
            self.ensure_configs()
            if self.active:
                self.restart()
            else:
                self.start()
        except RuntimeError:
            LOG.exception(
                _LE("Failed to enable vpn process on router %s"),
                self.id)

    def disable(self):
        """Disabling the process."""
        try:
            if self.active:
                self.stop()
            self.remove_config()
        except RuntimeError:
            LOG.exception(
                _LE("Failed to disable vpn process on router %s"),
                self.id)

    @abc.abstractmethod
    def restart(self):
        """Restart process."""

    @abc.abstractmethod
    def start(self):
        """Start process."""

    @abc.abstractmethod
    def stop(self):
        """Stop process."""

    def _extract_and_record_connection_status(self, status_output):
        if not status_output:
            self.connection_status = {}
            return
        for line in status_output.split('\n'):
            if self.STATUS_NOT_RUNNING_PATTERN.search(line):
                self.connection_status = {}
                break
            m = self.STATUS_PATTERN.search(line)
            if not m:
                continue
            connection_id = m.group(1)
            status = m.group(2)
            self._record_connection_status(connection_id,
                                           self.STATUS_MAP[status])

    def _record_connection_status(self, connection_id, status,
                                  force_status_update=False):
        conn_info = self.connection_status.get(connection_id)
        if not conn_info:
            self.connection_status[connection_id] = {
                'status': status,
                'updated_pending_status': force_status_update
            }
        else:
            conn_info['status'] = status
            if force_status_update:
                conn_info['updated_pending_status'] = True


class OpenSwanProcess(BaseSwanProcess):
    """OpenSwan Process manager class.

    This process class uses three commands
    (1) ipsec pluto:  IPsec IKE keying daemon
    (2) ipsec addconn: Adds new ipsec addconn
    (3) ipsec whack:  control interface for IPSEC keying daemon
    """
    def __init__(self, conf, process_id, vpnservice, namespace):
        super(OpenSwanProcess, self).__init__(conf, process_id,
                                              vpnservice, namespace)
        self.secrets_file = os.path.join(
            self.etc_dir, 'ipsec.secrets')
        self.config_file = os.path.join(
            self.etc_dir, 'ipsec.conf')
        self.pid_path = os.path.join(
            self.config_dir, 'var', 'run', 'pluto')

    def _execute(self, cmd, check_exit_code=True, extra_ok_codes=None):
        """Execute command on namespace."""
        ip_wrapper = ip_lib.IPWrapper(namespace=self.namespace)
        return ip_wrapper.netns.execute(cmd, check_exit_code=check_exit_code,
                                        extra_ok_codes=extra_ok_codes)

    def ensure_configs(self):
        """Generate config files which are needed for OpenSwan.

        If there is no directory, this function will create
        dirs.
        """
        self.ensure_config_dir(self.vpnservice)
        self.ensure_config_file(
            'ipsec.conf',
            self.conf.openswan.ipsec_config_template,
            self.vpnservice)
        self.ensure_config_file(
            'ipsec.secrets',
            self.conf.openswan.ipsec_secret_template,
            self.vpnservice,
            0o600)

    def get_status(self):
        return self._execute([self.binary,
                              'whack',
                              '--ctlbase',
                              self.pid_path,
                              '--status'], extra_ok_codes=[1, 3])

    def restart(self):
        """Restart the process."""
        self.stop()
        self.start()
        return

    def _resolve_fqdn(self, fqdn):
        # The first addrinfo member from the list returned by
        # socket.getaddrinfo is used for the address resolution.
        # The code doesn't filter for ipv4 or ipv6 address.
        try:
            addrinfo = socket.getaddrinfo(fqdn, None)[0]
            return addrinfo[-1][0]
        except socket.gaierror:
            LOG.exception(_LE("Peer address %s cannot be resolved"), fqdn)

    def _get_nexthop(self, address, connection_id):
        # check if address is an ip address or fqdn
        invalid_ip_address = attributes._validate_ip_address(address)
        if invalid_ip_address:
            ip_addr = self._resolve_fqdn(address)
            if not ip_addr:
                self._record_connection_status(connection_id, constants.ERROR,
                                               force_status_update=True)
                raise vpnaas.VPNPeerAddressNotResolved(peer_address=address)
        else:
            ip_addr = address
        routes = self._execute(['ip', 'route', 'get', ip_addr])
        if routes.find('via') >= 0:
            return routes.split(' ')[2]
        return address

    def _virtual_privates(self):
        """Returns line of virtual_privates.

        virtual_private contains the networks
        that are allowed as subnet for the remote client.
        """
        virtual_privates = []
        nets = [self.vpnservice['subnet']['cidr']]
        for ipsec_site_conn in self.vpnservice['ipsec_site_connections']:
            nets += ipsec_site_conn['peer_cidrs']
        for net in nets:
            version = netaddr.IPNetwork(net).version
            virtual_privates.append('%%v%s:%s' % (version, net))
        return ','.join(virtual_privates)

    def start(self):
        """Start the process.

        Note: if there is not namespace yet,
        just do nothing, and wait next event.
        """
        if not self.namespace:
            return
        virtual_private = self._virtual_privates()
        # start pluto IKE keying daemon
        cmd = [self.binary,
               'pluto',
               '--ctlbase', self.pid_path,
               '--ipsecdir', self.etc_dir,
               '--use-netkey',
               '--uniqueids',
               '--nat_traversal',
               '--secretsfile', self.secrets_file,
               '--virtual_private', virtual_private]

        if self.conf.ipsec.enable_detailed_logging:
            cmd += ['--perpeerlog', '--perpeerlogbase', self.log_dir]
        self._execute(cmd)
        # add connections
        for ipsec_site_conn in self.vpnservice['ipsec_site_connections']:
            nexthop = self._get_nexthop(ipsec_site_conn['peer_address'],
                                        ipsec_site_conn['id'])
            self._execute([self.binary,
                           'addconn',
                           '--ctlbase', '%s.ctl' % self.pid_path,
                           '--defaultroutenexthop', nexthop,
                           '--config', self.config_file,
                           ipsec_site_conn['id']
                           ])
        # TODO(nati) fix this when openswan is fixed
        # Due to openswan bug, this command always exit with 3
        # start whack ipsec keying daemon
        self._execute([self.binary,
                       'whack',
                       '--ctlbase', self.pid_path,
                       '--listen',
                       ], check_exit_code=False)

        for ipsec_site_conn in self.vpnservice['ipsec_site_connections']:
            if not ipsec_site_conn['initiator'] == 'start':
                continue
            # initiate ipsec connection
            self._execute([self.binary,
                           'whack',
                           '--ctlbase', self.pid_path,
                           '--name', ipsec_site_conn['id'],
                           '--asynchronous',
                           '--initiate'
                           ])

    def disconnect(self):
        if not self.namespace:
            return
        if not self.vpnservice:
            return
        for conn_id in self.connection_status:
            self._execute([self.binary,
                           'whack',
                           '--ctlbase', self.pid_path,
                           '--name', '%s/0x1' % conn_id,
                           '--terminate'
                           ])

    def stop(self):
        # Stop process using whack
        # Note this will also stop pluto
        self.disconnect()
        self._execute([self.binary,
                       'whack',
                       '--ctlbase', self.pid_path,
                       '--shutdown',
                       ])
        self.connection_status = {}


class IPsecVpnDriverApi(object):
    """IPSecVpnDriver RPC api."""

    def __init__(self, topic):
        target = oslo_messaging.Target(topic=topic, version='1.0')
        self.client = n_rpc.get_client(target)

    def get_vpn_services_on_host(self, context, host):
        """Get list of vpnservices.

        The vpnservices including related ipsec_site_connection,
        ikepolicy and ipsecpolicy on this host
        """
        cctxt = self.client.prepare()
        return cctxt.call(context, 'get_vpn_services_on_host', host=host)

    def update_status(self, context, status):
        """Update local status.

        This method call updates status attribute of
        VPNServices.
        """
        cctxt = self.client.prepare()
        return cctxt.call(context, 'update_status', status=status)


@six.add_metaclass(abc.ABCMeta)
class IPsecDriver(device_drivers.DeviceDriver):
    """VPN Device Driver for IPSec.

    This class is designed for use with L3-agent now.
    However this driver will be used with another agent in future.
    so the use of "Router" is kept minimul now.
    Instead of router_id,  we are using process_id in this code.
    """

    # history
    #   1.0 Initial version
    target = oslo_messaging.Target(version='1.0')

    def __init__(self, vpn_service, host):
        # TODO(pc_m) Replace vpn_service with config arg, once all driver
        # implemenations no longer need vpn_service.
        self.conf = vpn_service.conf
        self.host = host
        self.conn = n_rpc.create_connection(new=True)
        self.context = context.get_admin_context_without_session()
        self.topic = topics.IPSEC_AGENT_TOPIC
        node_topic = '%s.%s' % (self.topic, self.host)

        self.processes = {}
        self.routers = {}
        self.process_status_cache = {}

        self.endpoints = [self]
        self.conn.create_consumer(node_topic, self.endpoints, fanout=False)
        self.conn.consume_in_threads()
        self.agent_rpc = IPsecVpnDriverApi(topics.IPSEC_DRIVER_TOPIC)
        self.process_status_cache_check = loopingcall.FixedIntervalLoopingCall(
            self.report_status, self.context)
        self.process_status_cache_check.start(
            interval=self.conf.ipsec.ipsec_status_check_interval)

    def get_namespace(self, router_id):
        """Get namespace of router.

        :router_id: router_id
        :returns: namespace string.
            Note: If the router is a DVR, then the SNAT namespace will be
                  provided. If the router does not exist, return None.
        """
        router = self.routers.get(router_id)
        if not router:
            return
        # For DVR, use SNAT namespace
        # TODO(pcm): Use router object method to tell if DVR, when available
        if router.router['distributed']:
            return router.snat_namespace.name
        else:
            return router.ns_name

    def get_router_based_iptables_manager(self, router):
        """Returns router based iptables manager

        In DVR routers the IPsec VPN service should run inside
        the snat namespace. So the iptables manager used for
        snat namespace is different from the iptables manager
        used for the qr namespace in a non dvr based router.

        This function will check the router type and then will
        return the right iptables manager. If DVR enabled router
        it will return the snat_iptables_manager otherwise it will
        return the legacy iptables_manager.
        """
        # TODO(pcm): Use router object method to tell if DVR, when available
        if router.router['distributed']:
            return router.snat_iptables_manager
        else:
            return router.iptables_manager

    def add_nat_rule(self, router_id, chain, rule, top=False):
        """Add nat rule in namespace.

        :param router_id: router_id
        :param chain: a string of chain name
        :param rule: a string of rule
        :param top: if top is true, the rule
            will be placed on the top of chain
            Note if there is no rotuer, this method do nothing
        """
        router = self.routers.get(router_id)
        if not router:
            return
        iptables_manager = self.get_router_based_iptables_manager(router)
        iptables_manager.ipv4['nat'].add_rule(chain, rule, top=top)

    def remove_nat_rule(self, router_id, chain, rule, top=False):
        """Remove nat rule in namespace.

        :param router_id: router_id
        :param chain: a string of chain name
        :param rule: a string of rule
        :param top: unused
            needed to have same argument with add_nat_rule
        """
        router = self.routers.get(router_id)
        if not router:
            return
        iptables_manager = self.get_router_based_iptables_manager(router)
        iptables_manager.ipv4['nat'].remove_rule(chain, rule, top=top)
    
    def _exec_ip_rule_on_ns(self, namespace, cmd, check_exit_code=True, extra_ok_codes=None):
        """Execute command on namespace."""
        ip_wrapper = ip_lib.IPWrapper(namespace=namespace)
        return ip_wrapper.netns.execute(cmd, check_exit_code=check_exit_code,
                                        extra_ok_codes=extra_ok_codes)
    
    def add_ip_rule(self, router_id, src_cidr, dest_cidr):
        router = self.routers.get(router_id)
        if not router: 
            return
        vpn_idx = self._get_vpn_idx(router, src_cidr)
        if not router.floating_ips:
            return
        cmd = ['ip', 'rule', 'add', 'from', src_cidr, 'to', dest_cidr,
               'lookup', vpn_idx, 'pref', DVR_VPN_IP_RULE_PRIORITY] 
        self._exec_ip_rule_on_ns(router.ns_name, cmd)
    
    def remove_ip_rule(self, router_id, src_cidr, dest_cidr):
        router = self.routers.get(router_id)
        if not router: 
            return
        vpn_idx = self._get_vpn_idx(router, src_cidr)
        cmd = ['ip', 'rule', 'del', 'from', src_cidr, 'to', dest_cidr,
               'lookup', vpn_idx, 'pref', DVR_VPN_IP_RULE_PRIORITY] 
        self._exec_ip_rule_on_ns(router.ns_name, cmd)
        
    def _get_vpn_idx(self, ri, subnet_cidr):
        router = ri.router
        for interface in router['_interfaces']:
            for subnet in interface['subnets']:
                if subnet['cidr'] == subnet_cidr:
                    return ri._get_snat_idx(subnet['gateway_ip'] + '/24')
        
    def iptables_apply(self, router_id):
        """Apply IPtables.

        :param router_id: router_id
        This method do nothing if there is no router
        """
        router = self.routers.get(router_id)
        if not router:
            return
        iptables_manager = self.get_router_based_iptables_manager(router)
        iptables_manager.apply()

    def _update_nat(self, vpnservice, func):
        """Setting up nat rule in iptables.

        We need to setup nat rule for ipsec packet.
        :param vpnservice: vpnservices
        :param func: self.add_nat_rule or self.remove_nat_rule
        """
        local_cidr = vpnservice['subnet']['cidr']
        # This ipsec rule is not needed for ipv6.
        if netaddr.IPNetwork(local_cidr).version == 6:
            return

        router_id = vpnservice['router_id']
        for ipsec_site_connection in vpnservice['ipsec_site_connections']:
            for peer_cidr in ipsec_site_connection['peer_cidrs']:
                func(
                    router_id,
                    'POSTROUTING',
                    '-s %s -d %s -m policy '
                    '--dir out --pol ipsec '
                    '-j ACCEPT ' % (local_cidr, peer_cidr),
                    top=True)
        self.iptables_apply(router_id)
        
    def _update_ip_rule(self, vpnservice, func):
        """Setting up ip rule in qrouter-ns.
        We need to setup ip rule for ipsec packet to avoid fip affect.
        :param vpnservice: vpnservices
        :param func: self.add_ip_rule or self.remove_ip_rule
        """
        local_cidr = vpnservice['subnet']['cidr']
        # This ipsec rule is not needed for ipv6.
        if netaddr.IPNetwork(local_cidr).version == 6:
            return
        router_id = vpnservice['router_id']
        for ipsec_site_connection in vpnservice['ipsec_site_connections']:
            for peer_cidr in ipsec_site_connection['peer_cidrs']:
                func(router_id, local_cidr, peer_cidr)

    def vpnservice_updated(self, context, **kwargs):
        """Vpnservice updated rpc handler

        VPN Service Driver will call this method
        when vpnservices updated.
        Then this method start sync with server.
        """
        router = kwargs.get('router', None)
        self.sync(context, [router] if router else [])

    @abc.abstractmethod
    def create_process(self, process_id, vpnservice, namespace):
        pass

    def ensure_process(self, process_id, vpnservice=None):
        """Ensuring process.

        If the process doesn't exist, it will create process
        and store it in self.processs
        """
        process = self.processes.get(process_id)
        if not process or not process.namespace:
            namespace = self.get_namespace(process_id)
            process = self.create_process(
                process_id,
                vpnservice,
                namespace)
            self.processes[process_id] = process
        elif vpnservice:
            process.update_vpnservice(vpnservice)
        return process

    def create_router(self, router):
        """Handling create router event.

        Agent calls this method, when the process namespace is ready.
        Note: process_id == router_id == vpnservice_id
        """
        process_id = router.router_id
        self.routers[process_id] = router
        if cfg.CONF.agent_mode == 'dvr':
            return
        if process_id in self.processes:
            # In case of vpnservice is created
            # before router's namespace
            process = self.processes[process_id]
            self._update_nat(process.vpnservice, self.add_nat_rule)
            # Don't run ipsec process for backup HA router
            if router.router['ha'] and router.ha_state == 'backup':
                return
            process.enable()

    def destroy_process(self, process_id):
        """Destroy process.

        Disable the process, remove the nat rule, and remove the process
        manager for the processes that no longer are running vpn service.
        """
        if process_id in self.processes:
            process = self.processes[process_id]
            process.disable()
            vpnservice = process.vpnservice
            if vpnservice and cfg.CONF.agent_mode == 'dvr':
                self._update_ip_rule(vpnservice, self.remove_ip_rule)
            elif vpnservice and cfg.CONF.agent_mode == 'dvr_snat':
                self._update_nat(vpnservice, self.remove_nat_rule)
            del self.processes[process_id]

    def destroy_router(self, process_id):
        """Handling destroy_router event.

        Agent calls this method, when the process namespace
        is deleted.
        """
        self.destroy_process(process_id)
        if process_id in self.routers:
            del self.routers[process_id]

    def get_process_status_cache(self, process):
        if not self.process_status_cache.get(process.id):
            self.process_status_cache[process.id] = {
                'status': None,
                'id': process.vpnservice['id'],
                'updated_pending_status': False,
                'ipsec_site_connections': {}}
        return self.process_status_cache[process.id]

    def is_status_updated(self, process, previous_status):
        if process.updated_pending_status:
            return True
        if process.status != previous_status['status']:
            return True
        if (process.connection_status != 
            previous_status['ipsec_site_connections']):
            return True

    def unset_updated_pending_status(self, process):
        process.updated_pending_status = False
        for connection_status in process.connection_status.values():
            connection_status['updated_pending_status'] = False

    def copy_process_status(self, process):
        return {
            'id': process.vpnservice['id'],
            'status': process.status,
            'updated_pending_status': process.updated_pending_status,
            'ipsec_site_connections': copy.deepcopy(process.connection_status)
        }

    def update_downed_connections(self, process_id, new_status):
        """Update info to be reported, if connections just went down.

        If there is no longer any information for a connection, because it
        has been removed (e.g. due to an admin down of VPN service or IPSec
        connection), but there was previous status information for the
        connection, mark the connection as down for reporting purposes.
        """
        if process_id in self.process_status_cache:
            for conn in self.process_status_cache[process_id][IPSEC_CONNS]:
                if conn not in new_status[IPSEC_CONNS]:
                    new_status[IPSEC_CONNS][conn] = {
                        'status': constants.DOWN,
                        'updated_pending_status': True
                    }

    def report_status(self, context):
        status_changed_vpn_services = []
        for process in self.processes.values():
            previous_status = self.get_process_status_cache(process)
            if self.is_status_updated(process, previous_status):
                new_status = self.copy_process_status(process)
                self.update_downed_connections(process.id, new_status)
                status_changed_vpn_services.append(new_status)
                self.process_status_cache[process.id] = (
                    self.copy_process_status(process))
                # We need unset updated_pending status after it
                # is reported to the server side
                self.unset_updated_pending_status(process)

        if status_changed_vpn_services:
            self.agent_rpc.update_status(
                context,
                status_changed_vpn_services)

    @lockutils.synchronized('vpn-agent', 'neutron-')
    def sync(self, context, routers):
        """Sync status with server side.

        :param context: context object for RPC call
        :param routers: Router objects which is created in this sync event

        There could be many failure cases should be
        considered including the followings.
        1) Agent class restarted
        2) Failure on process creation
        3) VpnService is deleted during agent down
        4) RPC failure

        In order to handle, these failure cases,
        This driver takes simple sync strategies.
        """
        vpnservices = self.agent_rpc.get_vpn_services_on_host(
            context, self.host)
        router_ids = [vpnservice['router_id'] for vpnservice in vpnservices]
        sync_router_ids = [router['id'] for router in routers]

        self._sync_vpn_processes(vpnservices, sync_router_ids)
        self._delete_vpn_processes(sync_router_ids, router_ids)
        self._cleanup_stale_vpn_processes(router_ids)

        self.report_status(context)

    def _sync_vpn_processes(self, vpnservices, sync_router_ids):
        # Ensure the ipsec process is enabled only for
        # - the vpn services which are not yet in self.processes
        # - vpn services whose router id is in 'sync_router_ids'
        for vpnservice in vpnservices:
            if vpnservice['router_id'] not in self.processes or (
                    vpnservice['router_id'] in sync_router_ids):
                if cfg.CONF.agent_mode != 'dvr':
                    process = self.ensure_process(vpnservice['router_id'],
                                                  vpnservice=vpnservice)
                if cfg.CONF.agent_mode == 'dvr':
                    self._update_ip_rule(vpnservice, self.add_ip_rule)
                    continue
                elif cfg.CONF.agent_mode == 'dvr_snat':
                    self._update_nat(vpnservice, self.add_nat_rule)
                else:
                    continue
                router = self.routers.get(vpnservice['router_id'])
                if not router:
                    continue
                # For HA router, spawn vpn process on master router
                # and terminate vpn process on backup router
                if router.router['ha'] and router.ha_state == 'backup':
                    process.disable()
                else:
                    process.update()

    def _delete_vpn_processes(self, sync_router_ids, vpn_router_ids):
        # Delete any IPSec processes that are
        # associated with routers, but are not running the VPN service.
        for process_id in sync_router_ids:
            if process_id not in vpn_router_ids:
                self.destroy_process(process_id)

    def _cleanup_stale_vpn_processes(self, vpn_router_ids):
        # Delete any IPSec processes running
        # VPN that do not have an associated router.
        process_ids = [pid for pid in self.processes
                       if pid not in vpn_router_ids]
        for process_id in process_ids:
            self.destroy_process(process_id)


class OpenSwanDriver(IPsecDriver):
    def create_process(self, process_id, vpnservice, namespace):
        return OpenSwanProcess(
            self.conf,
            process_id,
            vpnservice,
            namespace)
