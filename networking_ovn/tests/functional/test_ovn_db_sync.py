# Copyright 2016 Red Hat, Inc.
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

import mock

from neutron_lib import context
from oslo_utils import uuidutils

from networking_ovn.common import acl as acl_utils
from networking_ovn.common import constants as ovn_const
from networking_ovn.common import utils
from networking_ovn import ovn_db_sync
from networking_ovn.ovsdb import commands as cmd
from networking_ovn.tests.functional import base
from neutron.agent.ovsdb.native import idlutils
from neutron.services.segments import db as segments_db
from neutron.tests.unit.api import test_extensions
from neutron.tests.unit.extensions import test_extraroute
from neutron.tests.unit.extensions import test_securitygroup
from neutron_lib.api.definitions import l3
from neutron_lib import constants
from neutron_lib.plugins import directory


class TestOvnNbSync(base.TestOVNFunctionalBase):

    def setUp(self):
        super(TestOvnNbSync, self).setUp()
        ext_mgr = test_extraroute.ExtraRouteTestExtensionManager()
        self.ext_api = test_extensions.setup_extensions_middleware(ext_mgr)
        sg_mgr = test_securitygroup.SecurityGroupTestExtensionManager()
        self._sg_api = test_extensions.setup_extensions_middleware(sg_mgr)
        self.create_lswitches = []
        self.create_lswitch_ports = []
        self.create_lrouters = []
        self.create_lrouter_ports = []
        self.create_lrouter_routes = []
        self.create_lrouter_nats = []
        self.update_lrouter_ports = []
        self.create_acls = []
        self.delete_lswitches = []
        self.delete_lswitch_ports = []
        self.delete_lrouters = []
        self.delete_lrouter_ports = []
        self.delete_lrouter_routes = []
        self.delete_lrouter_nats = []
        self.delete_acls = []
        self.create_address_sets = []
        self.delete_address_sets = []
        self.update_address_sets = []
        self.expected_dhcp_options_rows = []
        self.reset_lport_dhcpv4_options = []
        self.reset_lport_dhcpv6_options = []
        self.stale_lport_dhcpv4_options = []
        self.stale_lport_dhcpv6_options = []
        self.orphaned_lport_dhcp_options = []
        self.lport_dhcpv4_disabled = {}
        self.lport_dhcpv6_disabled = {}
        self.missed_dhcp_options = []
        self.dirty_dhcp_options = []
        self.lport_dhcp_ignored = []
        self.match_old_mac_dhcp_subnets = []

    def _api_for_resource(self, resource):
        if resource in ['security-groups']:
            return self._sg_api
        else:
            return super(TestOvnNbSync, self)._api_for_resource(resource)

    def _create_resources(self, restart_ovsdb_processes=False):
        n1 = self._make_network(self.fmt, 'n1', True)
        res = self._create_subnet(self.fmt, n1['network']['id'],
                                  '10.0.0.0/24')
        n1_s1 = self.deserialize(self.fmt, res)
        res = self._create_subnet(self.fmt, n1['network']['id'],
                                  '2001:dba::/64', ip_version=6,
                                  enable_dhcp=True)
        n1_s2 = self.deserialize(self.fmt, res)
        res = self._create_subnet(self.fmt, n1['network']['id'],
                                  '2001:dbb::/64', ip_version=6,
                                  ipv6_address_mode='slaac',
                                  ipv6_ra_mode='slaac')
        n1_s3 = self.deserialize(self.fmt, res)
        self.expected_dhcp_options_rows.append({
            'cidr': '10.0.0.0/24',
            'external_ids': {'subnet_id': n1_s1['subnet']['id']},
            'options': {'server_id': '10.0.0.1',
                        'server_mac': '01:02:03:04:05:06',
                        'lease_time': str(12 * 60 * 60),
                        'mtu': str(n1['network']['mtu']),
                        'router': n1_s1['subnet']['gateway_ip']}})
        self.expected_dhcp_options_rows.append({
            'cidr': '2001:dba::/64',
            'external_ids': {'subnet_id': n1_s2['subnet']['id']},
            'options': {'server_id': '01:02:03:04:05:06'}})

        n1_s1_dhcp_options_uuid = (
            self.mech_driver._nb_ovn.get_subnet_dhcp_options(
                n1_s1['subnet']['id'])['uuid'])
        update_port_ids_v4 = []
        update_port_ids_v6 = []
        n1_port_dict = {}
        for p in ['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7']:
            port = self._make_port(self.fmt, n1['network']['id'],
                                   name='n1-' + p,
                                   device_owner='compute:None')
            n1_port_dict[p] = port['port']['id']
            lport_name = port['port']['id']
            lswitch_name = 'neutron-' + n1['network']['id']
            if p == 'p1':
                fake_subnet = {'cidr': '11.11.11.11/24'}
                dhcp_acls = acl_utils.add_acl_dhcp(port['port'], fake_subnet)
                for dhcp_acl in dhcp_acls:
                    self.create_acls.append(dhcp_acl)
            elif p == 'p2':
                self.delete_lswitch_ports.append((lport_name, lswitch_name))
                update_port_ids_v4.append(port['port']['id'])
                update_port_ids_v6.append(port['port']['id'])
                self.expected_dhcp_options_rows.append({
                    'cidr': '10.0.0.0/24',
                    'external_ids': {'subnet_id': n1_s1['subnet']['id'],
                                     'port_id': port['port']['id']},
                    'options': {'server_id': '10.0.0.1',
                                'server_mac': '01:02:03:04:05:06',
                                'lease_time': str(12 * 60 * 60),
                                'mtu': str(n1['network']['mtu']),
                                'router': n1_s1['subnet']['gateway_ip'],
                                'tftp_server': '20.0.0.20',
                                'dns_server': '8.8.8.8'}})
                self.expected_dhcp_options_rows.append({
                    'cidr': '2001:dba::/64',
                    'external_ids': {'subnet_id': n1_s2['subnet']['id'],
                                     'port_id': port['port']['id']},
                    'options': {'server_id': '01:02:03:04:05:06',
                                'domain_search': 'foo-domain'}})
                self.dirty_dhcp_options.append({
                    'subnet_id': n1_s1['subnet']['id'],
                    'port_id': lport_name})
                self.dirty_dhcp_options.append({
                    'subnet_id': n1_s2['subnet']['id'],
                    'port_id': lport_name})
            elif p == 'p3':
                self.delete_acls.append((lport_name, lswitch_name))
                self.reset_lport_dhcpv4_options.append(lport_name)
                self.lport_dhcpv6_disabled.update({
                    lport_name: n1_s1_dhcp_options_uuid})
                data = {'port': {
                    'extra_dhcp_opts': [{'ip_version': 6,
                                         'opt_name': 'dhcp_disabled',
                                         'opt_value': 'True'}]}}
                port_req = self.new_update_request('ports', data, lport_name)
                port_req.get_response(self.api)
            elif p == 'p4':
                self.lport_dhcpv4_disabled.update({
                    lport_name: n1_s1_dhcp_options_uuid})
                data = {'port': {
                    'extra_dhcp_opts': [{'ip_version': 4,
                                         'opt_name': 'dhcp_disabled',
                                         'opt_value': 'True'}]}}
                port_req = self.new_update_request('ports', data, lport_name)
                port_req.get_response(self.api)
                self.reset_lport_dhcpv6_options.append(lport_name)
            elif p == 'p5':
                self.stale_lport_dhcpv4_options.append({
                    'subnet_id': n1_s1['subnet']['id'],
                    'port_id': port['port']['id'],
                    'cidr': '10.0.0.0/24',
                    'options': {'server_id': '10.0.0.254',
                                'server_mac': '01:02:03:04:05:06',
                                'lease_time': str(3 * 60 * 60),
                                'mtu': str(n1['network']['mtu'] / 2),
                                'router': '10.0.0.254',
                                'tftp_server': '20.0.0.234',
                                'dns_server': '8.8.8.8'},
                    'external_ids': {'subnet_id': n1_s1['subnet']['id'],
                                     'port_id': port['port']['id']},
                    })
            elif p == 'p6':
                self.delete_lswitch_ports.append((lport_name, lswitch_name))
            elif p == 'p7':
                update_port_ids_v4.append(port['port']['id'])
                update_port_ids_v6.append(port['port']['id'])
                self.expected_dhcp_options_rows.append({
                    'cidr': '10.0.0.0/24',
                    'external_ids': {'subnet_id': n1_s1['subnet']['id'],
                                     'port_id': port['port']['id']},
                    'options': {'server_id': '10.0.0.1',
                                'server_mac': '01:02:03:04:05:06',
                                'lease_time': str(12 * 60 * 60),
                                'mtu': str(n1['network']['mtu']),
                                'router': n1_s1['subnet']['gateway_ip'],
                                'tftp_server': '20.0.0.20',
                                'dns_server': '8.8.8.8'}})
                self.expected_dhcp_options_rows.append({
                    'cidr': '2001:dba::/64',
                    'external_ids': {'subnet_id': n1_s2['subnet']['id'],
                                     'port_id': port['port']['id']},
                    'options': {'server_id': '01:02:03:04:05:06',
                                'domain_search': 'foo-domain'}})
                self.reset_lport_dhcpv4_options.append(lport_name)
                self.reset_lport_dhcpv6_options.append(lport_name)
        self.dirty_dhcp_options.append({'subnet_id': n1_s1['subnet']['id']})
        self.dirty_dhcp_options.append({'subnet_id': n1_s2['subnet']['id']})

        n2 = self._make_network(self.fmt, 'n2', True)
        res = self._create_subnet(self.fmt, n2['network']['id'],
                                  '20.0.0.0/24')
        n2_s1 = self.deserialize(self.fmt, res)
        res = self._create_subnet(self.fmt, n2['network']['id'],
                                  '2001:dbd::/64', ip_version=6)
        n2_s2 = self.deserialize(self.fmt, res)
        self.expected_dhcp_options_rows.append({
            'cidr': '20.0.0.0/24',
            'external_ids': {'subnet_id': n2_s1['subnet']['id']},
            'options': {'server_id': '20.0.0.1',
                        'server_mac': '01:02:03:04:05:06',
                        'lease_time': str(12 * 60 * 60),
                        'mtu': str(n2['network']['mtu']),
                        'router': n2_s1['subnet']['gateway_ip']}})
        self.expected_dhcp_options_rows.append({
            'cidr': '2001:dbd::/64',
            'external_ids': {'subnet_id': n2_s2['subnet']['id']},
            'options': {'server_id': '01:02:03:04:05:06'}})

        for p in ['p1', 'p2']:
            port = self._make_port(self.fmt, n2['network']['id'],
                                   name='n2-' + p,
                                   device_owner='compute:None')
            if p == 'p1':
                update_port_ids_v4.append(port['port']['id'])
                self.expected_dhcp_options_rows.append({
                    'cidr': '20.0.0.0/24',
                    'external_ids': {'subnet_id': n2_s1['subnet']['id'],
                                     'port_id': port['port']['id']},
                    'options': {'server_id': '20.0.0.1',
                                'server_mac': '01:02:03:04:05:06',
                                'lease_time': str(12 * 60 * 60),
                                'mtu': str(n1['network']['mtu']),
                                'router': n2_s1['subnet']['gateway_ip'],
                                'tftp_server': '20.0.0.20',
                                'dns_server': '8.8.8.8'}})
        self.missed_dhcp_options.extend([
            opts['uuid']
            for opts in self.mech_driver._nb_ovn.get_subnets_dhcp_options(
                [n2_s1['subnet']['id'], n2_s2['subnet']['id']])])

        for port_id in update_port_ids_v4:
            data = {'port': {'extra_dhcp_opts': [{'ip_version': 4,
                                                  'opt_name': 'tftp-server',
                                                  'opt_value': '20.0.0.20'},
                                                 {'ip_version': 4,
                                                  'opt_name': 'dns-server',
                                                  'opt_value': '8.8.8.8'}]}}
            port_req = self.new_update_request('ports', data, port_id)
            port_req.get_response(self.api)
        for port_id in update_port_ids_v6:
            data = {'port': {'extra_dhcp_opts': [{'ip_version': 6,
                                                  'opt_name': 'domain-search',
                                                  'opt_value': 'foo-domain'}]}}
            port_req = self.new_update_request('ports', data, port_id)
            port_req.get_response(self.api)

        # External network and subnet
        e1 = self._make_network(self.fmt, 'e1', True,
                                arg_list=('router:external',
                                          'provider:network_type',
                                          'provider:physical_network'),
                                **{'router:external': True,
                                   'provider:network_type': 'flat',
                                   'provider:physical_network': 'public'})
        self.assertEqual(True, e1['network']['router:external'])
        self.assertEqual('flat', e1['network']['provider:network_type'])
        self.assertEqual('public', e1['network']['provider:physical_network'])
        res = self._create_subnet(self.fmt, e1['network']['id'],
                                  '100.0.0.0/24', gateway_ip='100.0.0.254',
                                  allocation_pools=[{'start': '100.0.0.2',
                                                     'end': '100.0.0.253'}],
                                  enable_dhcp=False)
        e1_s1 = self.deserialize(self.fmt, res)

        self.create_lswitches.append('neutron-' + uuidutils.generate_uuid())
        self.create_lswitch_ports.append(('neutron-' +
                                          uuidutils.generate_uuid(),
                                          'neutron-' + n1['network']['id']))
        self.create_lswitch_ports.append(('neutron-' +
                                          uuidutils.generate_uuid(),
                                          'neutron-' + n1['network']['id']))
        self.delete_lswitches.append('neutron-' + n2['network']['id'])
        self.delete_lswitch_ports.append(
            (utils.ovn_provnet_port_name(e1['network']['id']),
             utils.ovn_name(e1['network']['id'])))

        r1 = self.l3_plugin.create_router(
            self.context,
            {'router': {
                'name': 'r1', 'admin_state_up': True,
                'tenant_id': self._tenant_id,
                'external_gateway_info': {
                    'enable_snat': True,
                    'network_id': e1['network']['id'],
                    'external_fixed_ips': [
                        {'ip_address': '100.0.0.2',
                         'subnet_id': e1_s1['subnet']['id']}]}}})
        self.l3_plugin.add_router_interface(
            self.context, r1['id'], {'subnet_id': n1_s1['subnet']['id']})
        r1_p2 = self.l3_plugin.add_router_interface(
            self.context, r1['id'], {'subnet_id': n1_s2['subnet']['id']})
        self.l3_plugin.add_router_interface(
            self.context, r1['id'], {'subnet_id': n1_s3['subnet']['id']})
        r1_p3 = self.l3_plugin.add_router_interface(
            self.context, r1['id'], {'subnet_id': n2_s1['subnet']['id']})
        self.update_lrouter_ports.append(('lrp-' + r1_p2['port_id'],
                                          'neutron-' + r1['id'],
                                          n1_s2['subnet']['gateway_ip']))
        self.delete_lrouter_ports.append(('lrp-' + r1_p3['port_id'],
                                          'neutron-' + r1['id']))
        self.delete_lrouter_ports.append(('lrp-' + r1['gw_port_id'],
                                          'neutron-' + r1['id']))
        self.l3_plugin.update_router(
            self.context, r1['id'],
            {'router': {'routes': [{'destination': '10.10.0.0/24',
                                    'nexthop': '20.0.0.10'},
                                   {'destination': '10.11.0.0/24',
                                    'nexthop': '20.0.0.11'}]}})
        r1_f1 = self.l3_plugin.create_floatingip(
            self.context, {'floatingip': {
                'tenant_id': self._tenant_id,
                'floating_network_id': e1['network']['id'],
                'floating_ip_address': '100.0.0.20',
                'port_id': n1_port_dict['p1']}})
        r1_f2 = self.l3_plugin.create_floatingip(
            self.context, {'floatingip': {
                'tenant_id': self._tenant_id,
                'floating_network_id': e1['network']['id'],
                'floating_ip_address': '100.0.0.21'}})
        self.l3_plugin.update_floatingip(
            self.context, r1_f2['id'], {'floatingip': {
                'port_id': n1_port_dict['p2']}})

        # update External subnet gateway ip
        data = {'subnet': {'gateway_ip': '100.0.0.1'}}
        subnet_req = self.new_update_request(
            'subnets', data, e1_s1['subnet']['id'])
        subnet_req.get_response(self.api)

        # Static routes
        self.create_lrouter_routes.append(('neutron-' + r1['id'],
                                           '10.12.0.0/24',
                                           '20.0.0.12'))
        self.create_lrouter_routes.append(('neutron-' + r1['id'],
                                           '10.13.0.0/24',
                                           '20.0.0.13'))
        self.delete_lrouter_routes.append(('neutron-' + r1['id'],
                                           '10.10.0.0/24',
                                           '20.0.0.10'))
        # Gateway default route
        self.delete_lrouter_routes.append(('neutron-' + r1['id'],
                                           '0.0.0.0/0',
                                           '100.0.0.1'))
        # Gateway sNATs
        self.create_lrouter_nats.append(('neutron-' + r1['id'],
                                         '100.0.0.100',
                                         '200.0.0.0/24', 'snat'))
        self.delete_lrouter_nats.append(('neutron-' + r1['id'],
                                         '100.0.0.2',
                                         '10.0.0.0/24', 'snat'))
        # Floating IPs
        self.create_lrouter_nats.append(('neutron-' + r1['id'],
                                         '100.0.0.200',
                                         '200.0.0.200', 'dnat_and_snat'))
        self.delete_lrouter_nats.append(('neutron-' + r1['id'],
                                         r1_f1['floating_ip_address'],
                                         r1_f1['fixed_ip_address'],
                                         'dnat_and_snat'))

        n4 = self._make_network(self.fmt, 'n4', True)
        res = self._create_subnet(self.fmt, n4['network']['id'],
                                  '40.0.0.0/24', enable_dhcp=False)
        n4_s1 = self.deserialize(self.fmt, res)
        n4_port_dict = {}
        for p in ['p1', 'p2', 'p3']:
            port = self._make_port(self.fmt, n4['network']['id'],
                                   name='n4-' + p,
                                   device_owner='compute:None')
            n4_port_dict[p] = port['port']['id']
            self.lport_dhcp_ignored.append(port['port']['id'])

        r2 = self.l3_plugin.create_router(
            self.context,
            {'router': {'name': 'r2', 'admin_state_up': True,
                        'tenant_id': self._tenant_id}})
        n1_prtr = self._make_port(self.fmt, n1['network']['id'],
                                  name='n1-p-rtr')
        self.l3_plugin.add_router_interface(
            self.context, r2['id'], {'port_id': n1_prtr['port']['id']})
        self.l3_plugin.add_router_interface(
            self.context, r2['id'], {'subnet_id': n4_s1['subnet']['id']})
        self.l3_plugin.update_router(
            self.context, r2['id'],
            {'router': {'routes': [{'destination': '10.20.0.0/24',
                                    'nexthop': '10.0.0.20'}],
                        'external_gateway_info': {
                        'enable_snat': False,
                        'network_id': e1['network']['id'],
                        'external_fixed_ips': [
                            {'ip_address': '100.0.0.3',
                             'subnet_id': e1_s1['subnet']['id']}]}}})
        self.l3_plugin.create_floatingip(
            self.context, {'floatingip': {
                'tenant_id': self._tenant_id,
                'floating_network_id': e1['network']['id'],
                'floating_ip_address': '100.0.0.30',
                'port_id': n4_port_dict['p1']}})
        self.l3_plugin.create_floatingip(
            self.context, {'floatingip': {
                'tenant_id': self._tenant_id,
                'floating_network_id': e1['network']['id'],
                'floating_ip_address': '100.0.0.31',
                'port_id': n4_port_dict['p2']}})
        # To test l3_plugin.disassociate_floatingips, associating floating IP
        # to port p3 and then deleting p3.
        self.l3_plugin.create_floatingip(
            self.context, {'floatingip': {
                'tenant_id': self._tenant_id,
                'floating_network_id': e1['network']['id'],
                'floating_ip_address': '100.0.0.32',
                'port_id': n4_port_dict['p3']}})
        self._delete('ports', n4_port_dict['p3'])

        self.create_lrouters.append('neutron-' + uuidutils.generate_uuid())
        self.create_lrouter_ports.append(('lrp-' + uuidutils.generate_uuid(),
                                          'neutron-' + r1['id']))
        self.create_lrouter_ports.append(('lrp-' + uuidutils.generate_uuid(),
                                          'neutron-' + r1['id']))
        self.delete_lrouters.append('neutron-' + r2['id'])

        address_set_name = n1_prtr['port']['security_groups'][0]
        self.create_address_sets.extend([('fake_sg', 'ip4'),
                                         ('fake_sg', 'ip6')])
        self.delete_address_sets.append((address_set_name, 'ip6'))
        address_adds = ['10.0.0.101', '10.0.0.102']
        address_dels = []
        for address in n1_prtr['port']['fixed_ips']:
            address_dels.append(address['ip_address'])
        self.update_address_sets.append((address_set_name, 'ip4',
                                         address_adds, address_dels))

        # Create a network and subnet with orphaned OVN resources.
        n3 = self._make_network(self.fmt, 'n3', True)
        res = self._create_subnet(self.fmt, n3['network']['id'],
                                  '30.0.0.0/24')
        n3_s1 = self.deserialize(self.fmt, res)
        res = self._create_subnet(self.fmt, n3['network']['id'],
                                  '2001:dbc::/64', ip_version=6)
        n3_s2 = self.deserialize(self.fmt, res)
        if not restart_ovsdb_processes:
            # Test using original mac when syncing.
            dhcp_mac_v4 = self.mech_driver._nb_ovn.get_subnet_dhcp_options(
                n3_s1['subnet']['id']).get('options', {}).get('server_mac')
            dhcp_mac_v6 = self.mech_driver._nb_ovn.get_subnet_dhcp_options(
                n3_s2['subnet']['id']).get('options', {}).get('server_id')
            self.assertTrue(dhcp_mac_v4 is not None)
            self.assertTrue(dhcp_mac_v6 is not None)
            self.match_old_mac_dhcp_subnets.append(n3_s1['subnet']['id'])
            self.match_old_mac_dhcp_subnets.append(n3_s2['subnet']['id'])
        else:
            dhcp_mac_v4 = '01:02:03:04:05:06'
            dhcp_mac_v6 = '01:02:03:04:05:06'
        self.expected_dhcp_options_rows.append({
            'cidr': '30.0.0.0/24',
            'external_ids': {'subnet_id': n3_s1['subnet']['id']},
            'options': {'server_id': '30.0.0.1',
                        'server_mac': dhcp_mac_v4,
                        'lease_time': str(12 * 60 * 60),
                        'mtu': str(n3['network']['mtu']),
                        'router': n3_s1['subnet']['gateway_ip']}})
        self.expected_dhcp_options_rows.append({
            'cidr': '2001:dbc::/64',
            'external_ids': {'subnet_id': n3_s2['subnet']['id']},
            'options': {'server_id': dhcp_mac_v6}})
        fake_port_id1 = uuidutils.generate_uuid()
        fake_port_id2 = uuidutils.generate_uuid()
        self.create_lswitch_ports.append(('neutron-' + fake_port_id1,
                                          'neutron-' + n3['network']['id']))
        self.create_lswitch_ports.append(('neutron-' + fake_port_id2,
                                          'neutron-' + n3['network']['id']))
        stale_dhcpv4_options1 = {
            'subnet_id': n3_s1['subnet']['id'],
            'port_id': fake_port_id1,
            'cidr': '30.0.0.0/24',
            'options': {'server_id': '30.0.0.254',
                        'server_mac': dhcp_mac_v4,
                        'lease_time': str(3 * 60 * 60),
                        'mtu': str(n3['network']['mtu'] / 2),
                        'router': '30.0.0.254',
                        'tftp_server': '30.0.0.234',
                        'dns_server': '8.8.8.8'},
            'external_ids': {'subnet_id': n3_s1['subnet']['id'],
                             'port_id': fake_port_id1},
            }
        self.stale_lport_dhcpv4_options.append(stale_dhcpv4_options1)
        stale_dhcpv4_options2 = stale_dhcpv4_options1.copy()
        stale_dhcpv4_options2.update({
            'port_id': fake_port_id2,
            'external_ids': {'subnet_id': n3_s1['subnet']['id'],
                             'port_id': fake_port_id2}})
        self.stale_lport_dhcpv4_options.append(stale_dhcpv4_options2)
        self.orphaned_lport_dhcp_options.append(fake_port_id2)
        stale_dhcpv6_options1 = {
            'subnet_id': n3_s2['subnet']['id'],
            'port_id': fake_port_id1,
            'cidr': '2001:dbc::/64',
            'options': {'server_id': dhcp_mac_v6,
                        'domain-search': 'foo-domain'},
            'external_ids': {'subnet_id': n3_s2['subnet']['id'],
                             'port_id': fake_port_id1},
            }
        self.stale_lport_dhcpv6_options.append(stale_dhcpv6_options1)
        stale_dhcpv6_options2 = stale_dhcpv6_options1.copy()
        stale_dhcpv6_options2.update({
            'port_id': fake_port_id2,
            'external_ids': {'subnet_id': n3_s2['subnet']['id'],
                             'port_id': fake_port_id2}})
        self.stale_lport_dhcpv6_options.append(stale_dhcpv6_options2)
        fake_port = {'id': fake_port_id1, 'network_id': n3['network']['id']}
        dhcp_acls = acl_utils.add_acl_dhcp(fake_port, n3_s1['subnet'])
        for dhcp_acl in dhcp_acls:
            self.create_acls.append(dhcp_acl)

    def _modify_resources_in_nb_db(self):
        fake_api = mock.MagicMock()
        fake_api.idl = self.monitor_nb_db_idl
        fake_api._tables = self.monitor_nb_db_idl.tables

        with self.nb_idl_transaction(fake_api, check_error=True) as txn:
            for lswitch_name in self.create_lswitches:
                external_ids = {ovn_const.OVN_NETWORK_NAME_EXT_ID_KEY:
                                lswitch_name}
                txn.add(cmd.AddLSwitchCommand(fake_api, lswitch_name, True,
                                              external_ids=external_ids))

            for lswitch_name in self.delete_lswitches:
                txn.add(cmd.DelLSwitchCommand(fake_api, lswitch_name, True))

            for lport_name, lswitch_name in self.create_lswitch_ports:
                external_ids = {ovn_const.OVN_PORT_NAME_EXT_ID_KEY:
                                lport_name}
                txn.add(cmd.AddLSwitchPortCommand(fake_api, lport_name,
                                                  lswitch_name, True,
                                                  external_ids=external_ids))

            for lport_name, lswitch_name in self.delete_lswitch_ports:
                txn.add(cmd.DelLSwitchPortCommand(fake_api, lport_name,
                                                  lswitch_name, True))

            for lrouter_name in self.create_lrouters:
                external_ids = {ovn_const.OVN_ROUTER_NAME_EXT_ID_KEY:
                                lrouter_name}
                txn.add(cmd.AddLRouterCommand(fake_api, lrouter_name, True,
                                              external_ids=external_ids))

            for lrouter_name in self.delete_lrouters:
                txn.add(cmd.DelLRouterCommand(fake_api, lrouter_name, True))

            for lrport, lrouter_name in self.create_lrouter_ports:
                txn.add(cmd.AddLRouterPortCommand(fake_api, lrport,
                                                  lrouter_name))

            for lrport, lrouter_name, networks in self.update_lrouter_ports:
                txn.add(cmd.UpdateLRouterPortCommand(
                    fake_api, lrport, True,
                    **{'networks': [networks]}))

            for lrport, lrouter_name in self.delete_lrouter_ports:
                txn.add(cmd.DelLRouterPortCommand(fake_api, lrport,
                                                  lrouter_name, True))

            for lrouter_name, ip_prefix, nexthop in self.create_lrouter_routes:
                txn.add(cmd.AddStaticRouteCommand(fake_api, lrouter_name,
                                                  ip_prefix=ip_prefix,
                                                  nexthop=nexthop))

            for lrouter_name, ip_prefix, nexthop in self.delete_lrouter_routes:
                txn.add(cmd.DelStaticRouteCommand(fake_api, lrouter_name,
                                                  ip_prefix, nexthop, True))

            for lrouter_name, external_ip, logical_ip, nat_type in(
                    self.create_lrouter_nats):
                txn.add(cmd.AddNATRuleInLRouterCommand(
                    fake_api, lrouter_name, external_ip=external_ip,
                    logical_ip=logical_ip, type=nat_type))

            for lrouter_name, external_ip, logical_ip, nat_type in(
                    self.delete_lrouter_nats):
                txn.add(cmd.DeleteNATRuleInLRouterCommand(
                    fake_api, lrouter_name, external_ip=external_ip,
                    logical_ip=logical_ip, type=nat_type, if_exists=True))

            for acl in self.create_acls:
                txn.add(cmd.AddACLCommand(fake_api, **acl))

            for lport_name, lswitch_name in self.delete_acls:
                txn.add(cmd.DelACLCommand(fake_api, lswitch_name,
                                          lport_name, True))

            for name, ip_version in self.create_address_sets:
                ovn_name = utils.ovn_addrset_name(name, ip_version)
                external_ids = {ovn_const.OVN_SG_NAME_EXT_ID_KEY: name}
                txn.add(cmd.AddAddrSetCommand(fake_api, ovn_name, True,
                                              external_ids=external_ids))

            for name, ip_version in self.delete_address_sets:
                ovn_name = utils.ovn_addrset_name(name, ip_version)
                txn.add(cmd.DelAddrSetCommand(fake_api, ovn_name,
                                              True))

            for name, ip_version, ip_adds, ip_dels in self.update_address_sets:
                ovn_name = utils.ovn_addrset_name(name, ip_version)
                txn.add(cmd.UpdateAddrSetCommand(fake_api, ovn_name,
                                                 ip_adds, ip_dels, True))

            for lport_name in self.reset_lport_dhcpv4_options:
                txn.add(cmd.SetLSwitchPortCommand(fake_api, lport_name, True,
                                                  dhcpv4_options=[]))

            for lport_name in self.reset_lport_dhcpv6_options:
                txn.add(cmd.SetLSwitchPortCommand(fake_api, lport_name, True,
                                                  dhcpv6_options=[]))

            for dhcp_opts in self.stale_lport_dhcpv4_options:
                dhcpv4_opts = txn.add(cmd.AddDHCPOptionsCommand(
                    fake_api, dhcp_opts['subnet_id'],
                    port_id=dhcp_opts['port_id'],
                    cidr=dhcp_opts['cidr'],
                    options=dhcp_opts['options'],
                    external_ids=dhcp_opts['external_ids'],
                    may_exists=False))
                if dhcp_opts['port_id'] in self.orphaned_lport_dhcp_options:
                    continue
                txn.add(cmd.SetLSwitchPortCommand(fake_api, lport_name, True,
                                                  dhcpv4_options=dhcpv4_opts))

            for dhcp_opts in self.stale_lport_dhcpv6_options:
                dhcpv6_opts = txn.add(cmd.AddDHCPOptionsCommand(
                    fake_api, dhcp_opts['subnet_id'],
                    port_id=dhcp_opts['port_id'],
                    cidr=dhcp_opts['cidr'],
                    options=dhcp_opts['options'],
                    external_ids=dhcp_opts['external_ids'],
                    may_exists=False))
                if dhcp_opts['port_id'] in self.orphaned_lport_dhcp_options:
                    continue
                txn.add(cmd.SetLSwitchPortCommand(fake_api, lport_name, True,
                                                  dhcpv6_options=dhcpv6_opts))

            for row_uuid in self.missed_dhcp_options:
                txn.add(cmd.DelDHCPOptionsCommand(fake_api, row_uuid))

            for dhcp_opts in self.dirty_dhcp_options:
                txn.add(cmd.AddDHCPOptionsCommand(
                    fake_api, dhcp_opts['subnet_id'],
                    port_id=dhcp_opts.get('port_id'),
                    external_ids={'subnet_id': dhcp_opts['subnet_id'],
                                  'port_id': dhcp_opts.get('port_id')},
                    options={'foo': 'bar'}))

            for port_id in self.lport_dhcpv4_disabled:
                txn.add(cmd.SetLSwitchPortCommand(
                    fake_api, port_id, True,
                    dhcpv4_options=[self.lport_dhcpv4_disabled[port_id]]))

            for port_id in self.lport_dhcpv6_disabled:
                txn.add(cmd.SetLSwitchPortCommand(
                    fake_api, port_id, True,
                    dhcpv6_options=[self.lport_dhcpv6_disabled[port_id]]))

    def _validate_networks(self, should_match=True):
        db_networks = self._list('networks')
        db_net_ids = [net['id'] for net in db_networks['networks']]
        db_provnet_ports = [utils.ovn_provnet_port_name(net['id'])
                            for net in db_networks['networks']
                            if net.get('provider:physical_network')]

        # Get the list of lswitch ids stored in the OVN plugin IDL
        _plugin_nb_ovn = self.mech_driver._nb_ovn
        plugin_lswitch_ids = [
            row.name.replace('neutron-', '') for row in (
                _plugin_nb_ovn._tables['Logical_Switch'].rows.values())]

        # Get the list of lswitch ids stored in the monitor IDL connection
        monitor_lswitch_ids = [
            row.name.replace('neutron-', '') for row in (
                self.monitor_nb_db_idl.tables['Logical_Switch'].rows.values())]

        # Get the list of provnet ports stored in the OVN plugin IDL
        plugin_provnet_ports = [row.name for row in (
            _plugin_nb_ovn._tables['Logical_Switch_Port'].rows.values())
            if row.name.startswith(ovn_const.OVN_PROVNET_PORT_NAME_PREFIX)]

        # Get the list of provnet ports stored in the monitor IDL connection
        monitor_provnet_ports = [row.name for row in (
            self.monitor_nb_db_idl.tables['Logical_Switch_Port'].rows.values())
            if row.name.startswith(ovn_const.OVN_PROVNET_PORT_NAME_PREFIX)]

        if should_match:
            self.assertItemsEqual(db_net_ids, plugin_lswitch_ids)
            self.assertItemsEqual(db_net_ids, monitor_lswitch_ids)
            self.assertItemsEqual(db_provnet_ports, plugin_provnet_ports)
            self.assertItemsEqual(db_provnet_ports, monitor_provnet_ports)
        else:
            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_net_ids,
                plugin_lswitch_ids)

            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_net_ids,
                monitor_lswitch_ids)

            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_provnet_ports,
                plugin_provnet_ports)

            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_provnet_ports,
                monitor_provnet_ports)

    def _validate_ports(self, should_match=True):
        db_ports = self._list('ports')
        db_port_ids = [port['id'] for port in db_ports['ports'] if
                       port['device_owner'] !=
                       constants.DEVICE_OWNER_FLOATINGIP]
        db_port_ids_dhcp_valid = set(
            port['id'] for port in db_ports['ports']
            if not port['device_owner'].startswith(
                constants.DEVICE_OWNER_PREFIXES) and
            port['id'] not in self.lport_dhcp_ignored)

        _plugin_nb_ovn = self.mech_driver._nb_ovn
        plugin_lport_ids = [
            row.name for row in (
                _plugin_nb_ovn._tables['Logical_Switch_Port'].rows.values())
            if ovn_const.OVN_PORT_NAME_EXT_ID_KEY in row.external_ids]
        plugin_lport_ids_dhcpv4_enabled = [
            row.name for row in (
                _plugin_nb_ovn._tables['Logical_Switch_Port'].rows.values())
            if row.dhcpv4_options]
        plugin_lport_ids_dhcpv6_enabled = [
            row.name for row in (
                _plugin_nb_ovn._tables['Logical_Switch_Port'].rows.values())
            if row.dhcpv6_options]

        monitor_lport_ids = [
            row.name for row in (
                self.monitor_nb_db_idl.tables['Logical_Switch_Port'].
                rows.values())
            if ovn_const.OVN_PORT_NAME_EXT_ID_KEY in row.external_ids]
        monitor_lport_ids_dhcpv4_enabled = [
            row.name for row in (
                _plugin_nb_ovn._tables['Logical_Switch_Port'].rows.values())
            if row.dhcpv4_options]
        monitor_lport_ids_dhcpv6_enabled = [
            row.name for row in (
                _plugin_nb_ovn._tables['Logical_Switch_Port'].rows.values())
            if row.dhcpv6_options]

        if should_match:
            self.assertItemsEqual(db_port_ids, plugin_lport_ids)
            self.assertItemsEqual(db_port_ids, monitor_lport_ids)

            expected_dhcpv4_options_ports_ids = (
                db_port_ids_dhcp_valid.difference(
                    set(self.lport_dhcpv4_disabled.keys())))
            self.assertItemsEqual(expected_dhcpv4_options_ports_ids,
                                  plugin_lport_ids_dhcpv4_enabled)
            self.assertItemsEqual(expected_dhcpv4_options_ports_ids,
                                  monitor_lport_ids_dhcpv4_enabled)

            expected_dhcpv6_options_ports_ids = (
                db_port_ids_dhcp_valid.difference(
                    set(self.lport_dhcpv6_disabled.keys())))
            self.assertItemsEqual(expected_dhcpv6_options_ports_ids,
                                  plugin_lport_ids_dhcpv6_enabled)
            self.assertItemsEqual(expected_dhcpv6_options_ports_ids,
                                  monitor_lport_ids_dhcpv6_enabled)
        else:
            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_port_ids,
                plugin_lport_ids)

            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_port_ids,
                monitor_lport_ids)

            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_port_ids,
                plugin_lport_ids_dhcpv4_enabled)

            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_port_ids,
                monitor_lport_ids_dhcpv4_enabled)

    def _validate_dhcp_opts(self, should_match=True):
        observed_plugin_dhcp_options_rows = []
        _plugin_nb_ovn = self.mech_driver._nb_ovn
        for row in _plugin_nb_ovn._tables['DHCP_Options'].rows.values():
            opts = dict(row.options)
            ids = dict(row.external_ids)
            if ids.get('subnet_id') not in self.match_old_mac_dhcp_subnets:
                if 'server_mac' in opts:
                    opts['server_mac'] = '01:02:03:04:05:06'
                else:
                    opts['server_id'] = '01:02:03:04:05:06'
            observed_plugin_dhcp_options_rows.append({
                'cidr': row.cidr, 'external_ids': row.external_ids,
                'options': opts})

        observed_monitor_dhcp_options_rows = []
        for row in self.monitor_nb_db_idl.tables['DHCP_Options'].rows.values():
            opts = dict(row.options)
            ids = dict(row.external_ids)
            if ids.get('subnet_id') not in self.match_old_mac_dhcp_subnets:
                if 'server_mac' in opts:
                    opts['server_mac'] = '01:02:03:04:05:06'
                else:
                    opts['server_id'] = '01:02:03:04:05:06'
            observed_monitor_dhcp_options_rows.append({
                'cidr': row.cidr, 'external_ids': row.external_ids,
                'options': opts})

        if should_match:
            self.assertItemsEqual(self.expected_dhcp_options_rows,
                                  observed_plugin_dhcp_options_rows)
            self.assertItemsEqual(self.expected_dhcp_options_rows,
                                  observed_monitor_dhcp_options_rows)
        else:
            self.assertRaises(
                AssertionError, self.assertItemsEqual,
                self.expected_dhcp_options_rows,
                observed_plugin_dhcp_options_rows)

            self.assertRaises(
                AssertionError, self.assertItemsEqual,
                self.expected_dhcp_options_rows,
                observed_monitor_dhcp_options_rows)

    def _build_acl_to_compare(self, acl):
        acl_to_compare = {}
        for acl_key in getattr(acl, "_data", {}):
            try:
                acl_to_compare[acl_key] = getattr(acl, acl_key)
            except AttributeError:
                pass
        return acl_to_compare

    def _validate_acls(self, should_match=True):
        # Get the neutron DB ACLs.
        db_acls = []
        sg_cache = {}
        subnet_cache = {}
        for db_port in self._list('ports')['ports']:
            acls = acl_utils.add_acls(self.plugin,
                                      context.get_admin_context(),
                                      db_port,
                                      sg_cache,
                                      subnet_cache)
            for acl in acls:
                acl.pop('lport')
                acl.pop('lswitch')
                db_acls.append(acl)

        # Get the list of ACLs stored in the OVN plugin IDL.
        _plugin_nb_ovn = self.mech_driver._nb_ovn
        plugin_acls = []
        for row in _plugin_nb_ovn._tables['Logical_Switch'].rows.values():
            for acl in getattr(row, 'acls', []):
                plugin_acls.append(self._build_acl_to_compare(acl))

        # Get the list of ACLs stored in the OVN monitor IDL.
        monitor_nb_ovn = self.monitor_nb_db_idl
        monitor_acls = []
        for row in monitor_nb_ovn.tables['Logical_Switch'].rows.values():
            for acl in getattr(row, 'acls', []):
                monitor_acls.append(self._build_acl_to_compare(acl))

        if should_match:
            self.assertItemsEqual(db_acls, plugin_acls)
            self.assertItemsEqual(db_acls, monitor_acls)
        else:
            self.assertRaises(
                AssertionError, self.assertItemsEqual,
                db_acls, plugin_acls)
            self.assertRaises(
                AssertionError, self.assertItemsEqual,
                db_acls, monitor_acls)

    def _validate_routers_and_router_ports(self, should_match=True):
        db_routers = self._list('routers')
        db_router_ids = []
        db_routes = {}
        db_nats = {}
        for db_router in db_routers['routers']:
            db_router_ids.append(db_router['id'])
            db_routes[db_router['id']] = [db_route['destination'] +
                                          db_route['nexthop']
                                          for db_route in db_router['routes']]
            db_nats[db_router['id']] = []
            if db_router.get(l3.EXTERNAL_GW_INFO):
                r_ip, gw_ip = self.l3_plugin.\
                    get_external_router_and_gateway_ip(self.context, db_router)
                # Add gateway default route and snats
                if gw_ip:
                    db_routes[db_router['id']].append('0.0.0.0/0' + gw_ip)
                if r_ip and utils.is_snat_enabled(db_router):
                    networks = self.l3_plugin.\
                        _get_v4_network_of_all_router_ports(self.context,
                                                            db_router['id'])
                    db_nats[db_router['id']].extend([r_ip + network + 'snat'
                                                     for network in networks])
        fips = self._list('floatingips')
        for fip in fips['floatingips']:
            if fip['router_id']:
                db_nats[fip['router_id']].append(fip['floating_ip_address'] +
                                                 fip['fixed_ip_address'] +
                                                 'dnat_and_snat')

        _plugin_nb_ovn = self.mech_driver._nb_ovn
        plugin_lrouter_ids = [
            row.name.replace('neutron-', '') for row in (
                _plugin_nb_ovn._tables['Logical_Router'].rows.values())]

        monitor_lrouter_ids = [
            row.name.replace('neutron-', '') for row in (
                self.monitor_nb_db_idl.tables['Logical_Router'].rows.values())]

        if should_match:
            self.assertItemsEqual(db_router_ids, plugin_lrouter_ids)
            self.assertItemsEqual(db_router_ids, monitor_lrouter_ids)
        else:
            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_router_ids,
                plugin_lrouter_ids)

            self.assertRaises(
                AssertionError, self.assertItemsEqual, db_router_ids,
                monitor_lrouter_ids)

        for router_id in db_router_ids:
            r_ports = self._list('ports',
                                 query_params='device_id=%s' % (router_id))
            r_port_ids = [p['id'] for p in r_ports['ports']]
            r_port_networks = {
                p['id']: self.l3_plugin.get_networks_for_lrouter_port(
                    self.context, p['fixed_ips']) for p in r_ports['ports']}
            r_routes = db_routes[router_id]
            r_nats = db_nats[router_id]

            try:
                lrouter = idlutils.row_by_value(
                    self.mech_driver._nb_ovn.idl, 'Logical_Router', 'name',
                    'neutron-' + str(router_id), None)
                lports = getattr(lrouter, 'ports', [])
                plugin_lrouter_port_ids = [lport.name.replace('lrp-', '')
                                           for lport in lports]
                plugin_lport_networks = {
                    lport.name.replace('lrp-', ''): lport.networks
                    for lport in lports}
                sroutes = getattr(lrouter, 'static_routes', [])
                plugin_routes = [sroute.ip_prefix + sroute.nexthop
                                 for sroute in sroutes]
                nats = getattr(lrouter, 'nat', [])
                plugin_nats = [nat.external_ip + nat.logical_ip + nat.type
                               for nat in nats]
            except idlutils.RowNotFound:
                plugin_lrouter_port_ids = []
                plugin_routes = []
                plugin_nats = []

            try:
                lrouter = idlutils.row_by_value(
                    self.monitor_nb_db_idl, 'Logical_Router', 'name',
                    'neutron-' + router_id, None)
                lports = getattr(lrouter, 'ports', [])
                monitor_lrouter_port_ids = [lport.name.replace('lrp-', '')
                                            for lport in lports]
                monitor_lport_networks = {
                    lport.name.replace('lrp-', ''): lport.networks
                    for lport in lports}
                sroutes = getattr(lrouter, 'static_routes', [])
                monitor_routes = [sroute.ip_prefix + sroute.nexthop
                                  for sroute in sroutes]
                nats = getattr(lrouter, 'nat', [])
                monitor_nats = [nat.external_ip + nat.logical_ip + nat.type
                                for nat in nats]
            except idlutils.RowNotFound:
                monitor_lrouter_port_ids = []
                monitor_routes = []
                monitor_nats = []

            if should_match:
                self.assertItemsEqual(r_port_ids, plugin_lrouter_port_ids)
                self.assertItemsEqual(r_port_ids, monitor_lrouter_port_ids)
                for p in plugin_lport_networks:
                    self.assertItemsEqual(r_port_networks[p],
                                          plugin_lport_networks[p])
                for p in monitor_lport_networks:
                    self.assertItemsEqual(r_port_networks[p],
                                          monitor_lport_networks[p])
                self.assertItemsEqual(r_routes, plugin_routes)
                self.assertItemsEqual(r_routes, monitor_routes)
                self.assertItemsEqual(r_nats, plugin_nats)
                self.assertItemsEqual(r_nats, monitor_nats)
            else:
                self.assertRaises(
                    AssertionError, self.assertItemsEqual, r_port_ids,
                    plugin_lrouter_port_ids)

                self.assertRaises(
                    AssertionError, self.assertItemsEqual, r_port_ids,
                    monitor_lrouter_port_ids)

                for _p in self.update_lrouter_ports:
                    p = _p[0].replace('lrp-', '')
                    if p in plugin_lport_networks:
                        self.assertRaises(
                            AssertionError, self.assertItemsEqual,
                            r_port_networks[p], plugin_lport_networks[p])
                    if p in monitor_lport_networks:
                        self.assertRaises(
                            AssertionError, self.assertItemsEqual,
                            r_port_networks[p], monitor_lport_networks[p])

                self.assertRaises(
                    AssertionError, self.assertItemsEqual, r_routes,
                    plugin_routes)

                self.assertRaises(
                    AssertionError, self.assertItemsEqual, r_routes,
                    monitor_routes)

                self.assertRaises(
                    AssertionError, self.assertItemsEqual, r_nats,
                    plugin_nats)

                self.assertRaises(
                    AssertionError, self.assertItemsEqual, r_nats,
                    monitor_nats)

    def _validate_address_sets(self, should_match=True):
        db_ports = self._list('ports')['ports']
        sgs = self._list('security-groups')['security_groups']
        db_sgs = {}
        for sg in sgs:
            for ip_version in ['ip4', 'ip6']:
                name = utils.ovn_addrset_name(sg['id'], ip_version)
                db_sgs[name] = []

        for port in db_ports:
            sg_ids = utils.get_lsp_security_groups(port)
            addresses = acl_utils.acl_port_ips(port)
            for sg_id in sg_ids:
                for ip_version in addresses:
                    name = utils.ovn_addrset_name(sg_id, ip_version)
                    db_sgs[name].extend(addresses[ip_version])

        _plugin_nb_ovn = self.mech_driver._nb_ovn
        nb_address_sets = _plugin_nb_ovn.get_address_sets()
        nb_sgs = {}
        for nb_sgid, nb_values in nb_address_sets.items():
            nb_sgs[nb_sgid] = nb_values['addresses']
        mn_sgs = {}
        for row in self.monitor_nb_db_idl.tables['Address_Set'].rows.values():
            mn_sgs[getattr(row, 'name')] = getattr(row, 'addresses')

        if should_match:
            self.assertItemsEqual(nb_sgs, db_sgs)
            self.assertItemsEqual(mn_sgs, db_sgs)
        else:
            self.assertRaises(AssertionError, self.assertItemsEqual,
                              nb_sgs, db_sgs)
            self.assertRaises(AssertionError, self.assertItemsEqual,
                              mn_sgs, db_sgs)

    def _validate_resources(self, should_match=True):
        self._validate_networks(should_match=should_match)
        self._validate_ports(should_match=should_match)
        self._validate_dhcp_opts(should_match=should_match)
        self._validate_acls(should_match=should_match)
        self._validate_routers_and_router_ports(should_match=should_match)
        self._validate_address_sets(should_match=should_match)

    def _sync_resources(self, mode):
        nb_synchronizer = ovn_db_sync.OvnNbSynchronizer(
            self.plugin, self.mech_driver._nb_ovn, mode, self.mech_driver)

        ctx = context.get_admin_context()
        nb_synchronizer.sync_address_sets(ctx)
        nb_synchronizer.sync_networks_ports_and_dhcp_opts(ctx)
        nb_synchronizer.sync_acls(ctx)
        nb_synchronizer.sync_routers_and_rports(ctx)

    def _test_ovn_nb_sync_helper(self, mode, modify_resources=True,
                                 restart_ovsdb_processes=False,
                                 should_match_after_sync=True):
        self._create_resources(restart_ovsdb_processes)
        self._validate_resources(should_match=True)

        if modify_resources:
            self._modify_resources_in_nb_db()

        if restart_ovsdb_processes:
            # Restart the ovsdb-server and plugin idl.
            # This causes a new ovsdb-server to be started with empty
            # OVN NB DB
            self.restart()

        if modify_resources or restart_ovsdb_processes:
            self._validate_resources(should_match=False)

        self._sync_resources(mode)
        self._validate_resources(should_match=should_match_after_sync)

    def test_ovn_nb_sync_repair(self):
        self._test_ovn_nb_sync_helper('repair')

    def test_ovn_nb_sync_repair_delete_ovn_nb_db(self):
        # In this test case, the ovsdb-server for OVN NB DB is restarted
        # with empty OVN NB DB.
        self._test_ovn_nb_sync_helper('repair', modify_resources=False,
                                      restart_ovsdb_processes=True)

    def test_ovn_nb_sync_log(self):
        self._test_ovn_nb_sync_helper('log', should_match_after_sync=False)

    def test_ovn_nb_sync_off(self):
        self._test_ovn_nb_sync_helper('off', should_match_after_sync=False)


class TestOvnSbSync(base.TestOVNFunctionalBase):

    def setUp(self):
        super(TestOvnSbSync, self).setUp(ovn_worker=False)
        self.segments_plugin = directory.get_plugin('segments')
        self.sb_synchronizer = ovn_db_sync.OvnSbSynchronizer(
            self.plugin, self.mech_driver._sb_ovn, self.mech_driver)
        self.ctx = context.get_admin_context()

    def get_additional_service_plugins(self):
        return {'segments': 'neutron.services.segments.plugin.Plugin'}

    def _sync_resources(self):
        self.sb_synchronizer.sync_hostname_and_physical_networks(self.ctx)

    def create_segment(self, network_id, physical_network, segmentation_id):
        segment_data = {'network_id': network_id,
                        'physical_network': physical_network,
                        'segmentation_id': segmentation_id,
                        'network_type': 'vlan',
                        'name': constants.ATTR_NOT_SPECIFIED,
                        'description': constants.ATTR_NOT_SPECIFIED}
        return self.segments_plugin.create_segment(
            self.ctx, segment={'segment': segment_data})

    def test_ovn_sb_sync_add_new_host(self):
        with self.network() as network:
            network_id = network['network']['id']
        self.create_segment(network_id, 'physnet1', 50)
        self.add_fake_chassis('host1', ['physnet1'])
        segment_hosts = segments_db.get_hosts_mapped_with_segments(self.ctx)
        self.assertFalse(segment_hosts)
        self._sync_resources()
        segment_hosts = segments_db.get_hosts_mapped_with_segments(self.ctx)
        self.assertEqual({'host1'}, segment_hosts)

    def test_ovn_sb_sync_update_existing_host(self):
        with self.network() as network:
            network_id = network['network']['id']
        segment = self.create_segment(network_id, 'physnet1', 50)
        segments_db.update_segment_host_mapping(
            self.ctx, 'host1', {segment['id']})
        segment_hosts = segments_db.get_hosts_mapped_with_segments(self.ctx)
        self.assertEqual({'host1'}, segment_hosts)
        self.add_fake_chassis('host1', ['physnet2'])
        self._sync_resources()
        segment_hosts = segments_db.get_hosts_mapped_with_segments(self.ctx)
        self.assertFalse(segment_hosts)

    def test_ovn_sb_sync_delete_stale_host(self):
        with self.network() as network:
            network_id = network['network']['id']
        segment = self.create_segment(network_id, 'physnet1', 50)
        segments_db.update_segment_host_mapping(
            self.ctx, 'host1', {segment['id']})
        segment_hosts = segments_db.get_hosts_mapped_with_segments(self.ctx)
        self.assertEqual({'host1'}, segment_hosts)
        # Since there is no chassis in the sb DB, host1 is the stale host
        # recorded in neutron DB. It should be deleted after sync.
        self._sync_resources()
        segment_hosts = segments_db.get_hosts_mapped_with_segments(self.ctx)
        self.assertFalse(segment_hosts)

    def test_ovn_sb_sync(self):
        with self.network() as network:
            network_id = network['network']['id']
        seg1 = self.create_segment(network_id, 'physnet1', 50)
        self.create_segment(network_id, 'physnet2', 51)
        segments_db.update_segment_host_mapping(
            self.ctx, 'host1', {seg1['id']})
        segments_db.update_segment_host_mapping(
            self.ctx, 'host2', {seg1['id']})
        segments_db.update_segment_host_mapping(
            self.ctx, 'host3', {seg1['id']})
        segment_hosts = segments_db.get_hosts_mapped_with_segments(self.ctx)
        self.assertEqual({'host1', 'host2', 'host3'}, segment_hosts)
        self.add_fake_chassis('host2', ['physnet2'])
        self.add_fake_chassis('host3', ['physnet3'])
        self.add_fake_chassis('host4', ['physnet1'])
        self._sync_resources()
        segment_hosts = segments_db.get_hosts_mapped_with_segments(self.ctx)
        # host1 should be cleared since it is not in the chassis DB. host3
        # should be cleared since there is no segment for mapping.
        self.assertEqual({'host2', 'host4'}, segment_hosts)


class TestOvnNbSyncOverTcp(TestOvnNbSync):
    def get_ovsdb_server_protocol(self):
        return 'tcp'


class TestOvnSbSyncOverTcp(TestOvnSbSync):
    def get_ovsdb_server_protocol(self):
        return 'tcp'


class TestOvnNbSyncOverSsl(TestOvnNbSync):
    def get_ovsdb_server_protocol(self):
        return 'ssl'


class TestOvnSbSyncOverSsl(TestOvnSbSync):
    def get_ovsdb_server_protocol(self):
        return 'ssl'
