# Copyright (c) 2015 Canonical, Inc.
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

import os
import shutil

from oslo_config import cfg
from oslo_log import log as logging

from neutron.agent.linux import ip_lib
from neutron.plugins.common import constants
from neutron_vpnaas.services.vpn.device_drivers import ipsec

LOG = logging.getLogger(__name__)
TEMPLATE_PATH = os.path.dirname(os.path.abspath(__file__))

strongswan_opts = [
    cfg.StrOpt(
        'ipsec_config_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/strongswan/ipsec.conf.template'),
        help=_('Template file for ipsec configuration.')),
    cfg.StrOpt(
        'strongswan_config_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/strongswan/strongswan.conf.template'),
        help=_('Template file for strongswan configuration.')),
    cfg.StrOpt(
        'ipsec_secret_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/strongswan/ipsec.secret.template'),
        help=_('Template file for ipsec secret configuration.')),
    cfg.StrOpt(
        'default_config_area',
        default=os.path.join(
            TEMPLATE_PATH,
            '/etc/strongswan.d'),
        help=_('The area where default StrongSwan configuration '
               'files are located.'))
]
cfg.CONF.register_opts(strongswan_opts, 'strongswan')

NS_WRAPPER = 'neutron-vpn-netns-wrapper'


class StrongSwanProcess(ipsec.BaseSwanProcess):

    # ROUTED means route created. (only for auto=route mode)
    # CONNECTING means route created, connection tunnel is negotiating.
    # INSTALLED means route created,
    #           also connection tunnel installed. (traffic can pass)

    DIALECT_MAP = dict(ipsec.BaseSwanProcess.DIALECT_MAP)

    STATUS_DICT = {
        'ROUTED': constants.DOWN,
        'CONNECTING': constants.DOWN,
        'INSTALLED': constants.ACTIVE
    }
    STATUS_RE = '([a-f0-9\-]+).* (ROUTED|CONNECTING|INSTALLED)'
    STATUS_NOT_RUNNING_RE = 'Command:.*ipsec.*status.*Exit code: [1|3] '

    def __init__(self, conf, process_id, vpnservice, namespace):
        self.DIALECT_MAP['v1'] = 'ikev1'
        self.DIALECT_MAP['v2'] = 'ikev2'
        super(StrongSwanProcess, self).__init__(conf, process_id,
                                                vpnservice, namespace)

    def _execute(self, cmd, check_exit_code=True, extra_ok_codes=None):
        """Execute command on namespace.

        This execute is wrapped by namespace wrapper.
        The namespace wrapper will bind /etc/ and /var/run
        """
        ip_wrapper = ip_lib.IPWrapper(namespace=self.namespace)
        return ip_wrapper.netns.execute(
            [NS_WRAPPER,
             '--mount_paths=/etc:%s/etc,/var/run:%s/var/run' % (
                 self.config_dir, self.config_dir),
             '--cmd=%s' % ','.join(cmd)],
            check_exit_code=check_exit_code,
            extra_ok_codes=extra_ok_codes)

    def copy_and_overwrite(self, from_path, to_path):
        if os.path.exists(to_path):
            shutil.rmtree(to_path)
        shutil.copytree(from_path, to_path)

    def ensure_configs(self):
        """Generate config files which are needed for StrongSwan.

        If there is no directory, this function will create
        dirs.
        """
        self.ensure_config_dir(self.vpnservice)
        self.ensure_config_file(
            'ipsec.conf',
            cfg.CONF.strongswan.ipsec_config_template,
            self.vpnservice)
        self.ensure_config_file(
            'strongswan.conf',
            cfg.CONF.strongswan.strongswan_config_template,
            self.vpnservice)
        self.ensure_config_file(
            'ipsec.secrets',
            cfg.CONF.strongswan.ipsec_secret_template,
            self.vpnservice,
            0o600)
        self.copy_and_overwrite(cfg.CONF.strongswan.default_config_area,
                                self._get_config_filename('strongswan.d'))

    def get_status(self):
        return self._execute([self.binary, 'status'],
                             extra_ok_codes=[1, 3])

    def restart(self):
        """Restart the process."""
        self.reload()

    def reload(self):
        """Reload the process.

        Sends a USR1 signal to ipsec starter which in turn reloads the whole
        configuration on the running IKE daemon charon based on the actual
        ipsec.conf. Currently established connections are not affected by
        configuration changes.
        """
        self._execute([self.binary, 'reload'])

    def start(self):
        """Start the process for only auto=route mode now.

        Note: if there is no namespace yet,
        just do nothing, and wait next event.
        """
        if not self.namespace:
            return
        self._execute([self.binary, 'start'])
        # initiate ipsec connection
        for ipsec_site_conn in self.vpnservice['ipsec_site_connections']:
            self._execute([self.binary, 'up', ipsec_site_conn['id']])

    def stop(self):
        self._execute([self.binary, 'stop'])
        self.connection_status = {}


class StrongSwanDriver(ipsec.IPsecDriver):

    def create_process(self, process_id, vpnservice, namespace):
        return StrongSwanProcess(
            self.conf,
            process_id,
            vpnservice,
            namespace)
