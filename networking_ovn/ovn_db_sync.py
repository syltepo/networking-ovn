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

from datetime import datetime
from eventlet import greenthread
import itertools
from neutron_lib.api.definitions import l3
from neutron_lib.api.definitions import provider_net as pnet
from neutron_lib import constants
from neutron_lib import context
from neutron_lib.plugins import directory
from neutron_lib.utils import helpers
from oslo_log import log

from neutron.services.segments import db as segments_db

from networking_ovn.common import acl as acl_utils
from networking_ovn.common import config
from networking_ovn.common import constants as const
from networking_ovn.common import utils
import six

LOG = log.getLogger(__name__)

SYNC_MODE_OFF = 'off'
SYNC_MODE_LOG = 'log'
SYNC_MODE_REPAIR = 'repair'


@six.add_metaclass(abc.ABCMeta)
class OvnDbSynchronizer(object):

    def __init__(self, core_plugin, ovn_api, ovn_driver):
        self.ovn_driver = ovn_driver
        self.ovn_api = ovn_api
        self.core_plugin = core_plugin

    def sync(self):
        greenthread.spawn_n(self._sync)

    @abc.abstractmethod
    def _sync(self):
        """Method to sync the OVN DB."""


class OvnNbSynchronizer(OvnDbSynchronizer):
    """Synchronizer class for NB."""

    def __init__(self, core_plugin, ovn_api, mode, ovn_driver):
        super(OvnNbSynchronizer, self).__init__(
            core_plugin, ovn_api, ovn_driver)
        self.mode = mode
        self.l3_plugin = directory.get_plugin(constants.L3)

    def _sync(self):
        if self.mode == SYNC_MODE_OFF:
            LOG.debug("Neutron sync mode is off")
            return

        # Initial delay until service is up
        greenthread.sleep(10)
        LOG.debug("Starting OVN-Northbound DB sync process")

        ctx = context.get_admin_context()
        self.sync_address_sets(ctx)
        self.sync_networks_ports_and_dhcp_opts(ctx)
        self.sync_acls(ctx)
        self.sync_routers_and_rports(ctx)

    @staticmethod
    def _get_attribute(obj, attribute):
        res = obj.get(attribute)
        if res is constants.ATTR_NOT_SPECIFIED:
            res = None
        return res

    def _create_network_in_ovn(self, net):
        physnet = self._get_attribute(net, pnet.PHYSICAL_NETWORK)
        segid = self._get_attribute(net, pnet.SEGMENTATION_ID)
        self.ovn_driver.create_network_in_ovn(net, {}, physnet, segid)

    def _create_port_in_ovn(self, ctx, port):
        # Remove any old ACLs for the port to avoid creating duplicate ACLs.
        self.ovn_api.delete_acl(
            utils.ovn_name(port['network_id']),
            port['id']).execute(check_error=True)

        # Create the port in OVN. This will include ACL and Address Set
        # updates as needed.
        ovn_port_info = self.ovn_driver.get_ovn_port_options(port)
        self.ovn_driver.create_port_in_ovn(port, ovn_port_info)

    def remove_common_acls(self, neutron_acls, nb_acls):
        """Take out common acls of the two acl dictionaries.

        @param   neutron_acls: neutron dictionary of port vs acls
        @type    neutron_acls: {}
        @param   nb_acls: nb dictionary of port vs acls
        @type    nb_acls: {}
        @return: Nothing, original dictionary modified
        """
        for port in neutron_acls.keys():
            for acl in list(neutron_acls[port]):
                if port in nb_acls and acl in nb_acls[port]:
                    neutron_acls[port].remove(acl)
                    nb_acls[port].remove(acl)

    def compute_address_set_difference(self, neutron_sgs, nb_sgs):
        neutron_sgs_name_set = set(neutron_sgs.keys())
        nb_sgs_name_set = set(nb_sgs.keys())
        sgnames_to_add = list(neutron_sgs_name_set - nb_sgs_name_set)
        sgnames_to_delete = list(nb_sgs_name_set - neutron_sgs_name_set)
        sgs_common = list(neutron_sgs_name_set & nb_sgs_name_set)
        sgs_to_update = {}
        for sg_name in sgs_common:
            neutron_addr_set = set(neutron_sgs[sg_name]['addresses'])
            nb_addr_set = set(nb_sgs[sg_name]['addresses'])
            addrs_to_add = list(neutron_addr_set - nb_addr_set)
            addrs_to_delete = list(nb_addr_set - neutron_addr_set)
            if addrs_to_add or addrs_to_delete:
                sgs_to_update[sg_name] = {'name': sg_name,
                                          'addrs_add': addrs_to_add,
                                          'addrs_remove': addrs_to_delete}
        return sgnames_to_add, sgnames_to_delete, sgs_to_update

    def get_acls(self, context):
        """create the list of ACLS in OVN.

        @param context: neutron_lib.context
        @type  context: object of type neutron_lib.context.Context
        @var   lswitch_names: List of lswitch names
        @var   acl_list: List of NB acls
        @var   acl_list_dict: Dictionary of acl-lists based on lport as key
        @return: acl_list-dict
        """
        lswitch_names = set([])
        for network in self.core_plugin.get_networks(context):
            lswitch_names.add(network['id'])
        acl_dict, ignore1, ignore2 = \
            self.ovn_api.get_acls_for_lswitches(lswitch_names)
        acl_list = list(itertools.chain(*acl_dict.values()))
        acl_list_dict = {}
        for acl in acl_list:
            key = acl['lport']
            if key in acl_list_dict:
                acl_list_dict[key].append(acl)
            else:
                acl_list_dict[key] = list([acl])
        return acl_list_dict

    def get_address_sets(self):
        return self.ovn_api.get_address_sets()

    def sync_address_sets(self, ctx):
        """Sync Address Sets between neutron and NB.

        @param ctx: neutron_lib.context
        @type  ctx: object of type neutron_lib.context.Context
        @var   db_ports: List of ports from neutron DB
        """
        LOG.debug('Address-Set-SYNC: started @ %s' % str(datetime.now()))

        neutron_sgs = {}
        with ctx.session.begin(subtransactions=True):
            db_sgs = self.core_plugin.get_security_groups(ctx)
            db_ports = self.core_plugin.get_ports(ctx)

        for sg in db_sgs:
            for ip_version in ['ip4', 'ip6']:
                name = utils.ovn_addrset_name(sg['id'], ip_version)
                neutron_sgs[name] = {
                    'name': name, 'addresses': [],
                    'external_ids': {const.OVN_SG_NAME_EXT_ID_KEY:
                                     sg['name']}}

        for port in db_ports:
            sg_ids = utils.get_lsp_security_groups(port)
            if port.get('fixed_ips') and sg_ids:
                addresses = acl_utils.acl_port_ips(port)
                for sg_id in sg_ids:
                    for ip_version in addresses:
                        name = utils.ovn_addrset_name(sg_id, ip_version)
                        neutron_sgs[name]['addresses'].extend(
                            addresses[ip_version])

        nb_sgs = self.get_address_sets()

        sgnames_to_add, sgnames_to_delete, sgs_to_update =\
            self.compute_address_set_difference(neutron_sgs, nb_sgs)

        LOG.debug('Address_Sets added %d, removed %d, updated %d',
                  len(sgnames_to_add), len(sgnames_to_delete),
                  len(sgs_to_update))

        if self.mode == SYNC_MODE_REPAIR:
            LOG.debug('Address-Set-SYNC: transaction started @ %s' %
                      str(datetime.now()))
            with self.ovn_api.transaction(check_error=True) as txn:
                for sgname in sgnames_to_add:
                    sg = neutron_sgs[sgname]
                    txn.add(self.ovn_api.create_address_set(**sg))
                for sgname, sg in sgs_to_update.items():
                    txn.add(self.ovn_api.update_address_set(**sg))
                for sgname in sgnames_to_delete:
                    txn.add(self.ovn_api.delete_address_set(name=sgname))
            LOG.debug('Address-Set-SYNC: transaction finished @ %s' %
                      str(datetime.now()))

    def sync_acls(self, ctx):
        """Sync ACLs between neutron and NB.

        @param ctx: neutron_lib.context
        @type  ctx: object of type neutron_lib.context.Context
        @var   db_ports: List of ports from neutron DB
        @var   neutron_acls: neutron dictionary of port
               vs list-of-acls
        @var   nb_acls: NB dictionary of port
               vs list-of-acls
        @var   subnet_cache: cache for subnets
        @return: Nothing
        """
        LOG.debug('ACL-SYNC: started @ %s' %
                  str(datetime.now()))

        db_ports = {}
        for port in self.core_plugin.get_ports(ctx):
            db_ports[port['id']] = port

        sg_cache = {}
        subnet_cache = {}
        neutron_acls = {}
        for port_id, port in db_ports.items():
            if utils.get_lsp_security_groups(port):
                acl_list = acl_utils.add_acls(self.core_plugin,
                                              ctx,
                                              port,
                                              sg_cache,
                                              subnet_cache)
                if port_id in neutron_acls:
                    neutron_acls[port_id].extend(acl_list)
                else:
                    neutron_acls[port_id] = acl_list

        nb_acls = self.get_acls(ctx)

        self.remove_common_acls(neutron_acls, nb_acls)

        num_acls_to_add = len(list(itertools.chain(*neutron_acls.values())))
        num_acls_to_remove = len(list(itertools.chain(*nb_acls.values())))
        if 0 != num_acls_to_add or 0 != num_acls_to_remove:
            LOG.warning('ACLs-to-be-added %(add)d '
                        'ACLs-to-be-removed %(remove)d',
                        {'add': num_acls_to_add,
                         'remove': num_acls_to_remove})

        if self.mode == SYNC_MODE_REPAIR:
            with self.ovn_api.transaction(check_error=True) as txn:
                for acla in list(itertools.chain(*neutron_acls.values())):
                    LOG.warning('ACL found in Neutron but not in '
                                'OVN DB for port %s', acla['lport'])
                    txn.add(self.ovn_api.add_acl(**acla))

            with self.ovn_api.transaction(check_error=True) as txn:
                for aclr in list(itertools.chain(*nb_acls.values())):
                    # Both lswitch and lport aren't needed within the ACL.
                    lswitchr = aclr.pop('lswitch').replace('neutron-', '')
                    lportr = aclr.pop('lport')
                    aclr_dict = {lportr: aclr}
                    LOG.warning('ACLs found in OVN DB but not in '
                                'Neutron for port %s', lportr)
                    txn.add(self.ovn_api.update_acls(
                        [lswitchr],
                        [lportr],
                        aclr_dict,
                        need_compare=False,
                        is_add_acl=False
                    ))

        LOG.debug('ACL-SYNC: finished @ %s' %
                  str(datetime.now()))

    def sync_routers_and_rports(self, ctx):
        """Sync Routers between neutron and NB.

        @param ctx: neutron_lib.context
        @type  ctx: object of type neutron_lib.context.Context
        @var   db_routers: List of Routers from neutron DB
        @var   db_router_ports: List of Router ports from neutron DB
        @var   lrouters: NB dictionary of logical routers and
               the corresponding logical router ports.
               vs list-of-acls
        @var   del_lrouters_list: List of Routers that need to be
               deleted from NB
        @var   del_lrouter_ports_list: List of Router ports that need to be
               deleted from NB
        @return: Nothing
        """
        if not config.is_ovn_l3():
            LOG.debug("OVN L3 mode is disabled, skipping "
                      "sync routers and router ports")
            return

        LOG.debug('OVN-NB Sync Routers and Router ports started @ %s' %
                  str(datetime.now()))

        db_routers = {}
        db_extends = {}
        db_router_ports = {}
        for router in self.l3_plugin.get_routers(ctx):
            db_routers[router['id']] = router
            db_extends[router['id']] = {}
            db_extends[router['id']]['routes'] = []
            db_extends[router['id']]['snats'] = []
            db_extends[router['id']]['fips'] = []
            if not router.get(l3.EXTERNAL_GW_INFO):
                continue
            r_ip, gw_ip = self.l3_plugin.get_external_router_and_gateway_ip(
                ctx, router)
            if gw_ip:
                db_extends[router['id']]['routes'].append(
                    {'destination': '0.0.0.0/0', 'nexthop': gw_ip})
            if r_ip and utils.is_snat_enabled(router):
                networks = self.l3_plugin._get_v4_network_of_all_router_ports(
                    ctx, router['id'])
                for network in networks:
                    db_extends[router['id']]['snats'].append({
                        'logical_ip': network,
                        'external_ip': r_ip,
                        'type': 'snat'})

        fips = self.l3_plugin.get_floatingips(
            ctx, {'router_id': list(db_routers.keys())})
        for fip in fips:
            db_extends[fip['router_id']]['fips'].append(
                {'external_ip': fip['floating_ip_address'],
                 'logical_ip': fip['fixed_ip_address'],
                 'type': 'dnat_and_snat'})
        interfaces = self.l3_plugin._get_sync_interfaces(
            ctx, db_routers.keys(), [constants.DEVICE_OWNER_ROUTER_INTF,
                                     constants.DEVICE_OWNER_ROUTER_GW])
        for interface in interfaces:
            db_router_ports[interface['id']] = interface
            db_router_ports[interface['id']]['networks'] = sorted(
                self.l3_plugin.get_networks_for_lrouter_port(
                    ctx, interface['fixed_ips']))
        lrouters = self.ovn_api.get_all_logical_routers_with_rports()

        del_lrouters_list = []
        del_lrouter_ports_list = []
        update_sroutes_list = []
        update_lrport_list = []
        update_snats_list = []
        update_fips_list = []
        for lrouter in lrouters:
            if lrouter['name'] in db_routers:
                for lrport, lrport_nets in lrouter['ports'].items():
                    if lrport in db_router_ports:
                        db_lrport_nets = db_router_ports[lrport]['networks']
                        if db_lrport_nets != sorted(lrport_nets):
                            update_lrport_list.append((
                                lrouter['name'], db_router_ports[lrport]))
                        del db_router_ports[lrport]
                    else:
                        del_lrouter_ports_list.append(
                            {'port': lrport, 'lrouter': lrouter['name']})
                if 'routes' in db_routers[lrouter['name']]:
                    db_routes = db_routers[lrouter['name']]['routes']
                else:
                    db_routes = []
                if 'routes' in db_extends[lrouter['name']]:
                    db_routes.extend(db_extends[lrouter['name']]['routes'])

                ovn_routes = lrouter['static_routes']
                add_routes, del_routes = helpers.diff_list_of_dict(
                    ovn_routes, db_routes)
                update_sroutes_list.append({'id': lrouter['name'],
                                            'add': add_routes,
                                            'del': del_routes})
                ovn_fips = lrouter['dnat_and_snats']
                db_fips = db_extends[lrouter['name']]['fips']
                add_fips, del_fips = helpers.diff_list_of_dict(
                    ovn_fips, db_fips)
                update_fips_list.append({'id': lrouter['name'],
                                         'add': add_fips,
                                         'del': del_fips})
                ovn_nats = lrouter['snats']
                db_snats = db_extends[lrouter['name']]['snats']
                add_snats, del_snats = helpers.diff_list_of_dict(
                    ovn_nats, db_snats)
                update_snats_list.append({'id': lrouter['name'],
                                          'add': add_snats,
                                          'del': del_snats})
                del db_routers[lrouter['name']]
            else:
                del_lrouters_list.append(lrouter)

        for r_id, router in db_routers.items():
            LOG.warning("Router found in Neutron but not in "
                        "OVN DB, router id=%s", router['id'])
            if self.mode == SYNC_MODE_REPAIR:
                try:
                    LOG.warning("Creating the router %s in OVN NB DB",
                                router['id'])
                    self.l3_plugin.create_lrouter_in_ovn(router)
                    if 'routes' in router:
                        update_sroutes_list.append(
                            {'id': router['id'], 'add': router['routes'],
                             'del': []})
                    if 'routes' in db_extends[router['id']]:
                        update_sroutes_list.append(
                            {'id': router['id'],
                             'add': db_extends[router['id']]['routes'],
                             'del': []})
                    if 'snats' in db_extends[router['id']]:
                        update_snats_list.append(
                            {'id': router['id'],
                             'add': db_extends[router['id']]['snats'],
                             'del': []})
                    if 'fips' in db_extends[router['id']]:
                        update_fips_list.append(
                            {'id': router['id'],
                             'add': db_extends[router['id']]['fips'],
                             'del': []})
                except RuntimeError:
                    LOG.warning("Create router in OVN NB failed for router %s",
                                router['id'])

        for rp_id, rrport in db_router_ports.items():
            LOG.warning("Router Port found in Neutron but not in OVN "
                        "DB, router port_id=%s", rrport['id'])
            if self.mode == SYNC_MODE_REPAIR:
                try:
                    LOG.warning("Creating the router port %s in OVN NB DB",
                                rrport['id'])
                    self.l3_plugin.create_lrouter_port_in_ovn(
                        ctx, rrport['device_id'], rrport)
                except RuntimeError:
                    LOG.warning("Create router port in OVN "
                                "NB failed for router port %s", rrport['id'])

        for router_id, rport in update_lrport_list:
            LOG.warning("Router Port port_id=%s needs to be updated "
                        "for networks changed",
                        rport['id'])
            if self.mode == SYNC_MODE_REPAIR:
                try:
                    LOG.warning(
                        "Updating networks on router port %s in OVN NB DB",
                        rport['id'])
                    self.l3_plugin.update_lrouter_port_in_ovn(
                        ctx, router_id, rport, rport['networks'])
                except RuntimeError:
                    LOG.warning("Update router port networks in OVN "
                                "NB failed for router port %s", rport['id'])

        with self.ovn_api.transaction(check_error=True) as txn:
            for lrouter in del_lrouters_list:
                LOG.warning("Router found in OVN but not in "
                            "Neutron, router id=%s", lrouter['name'])
                if self.mode == SYNC_MODE_REPAIR:
                    LOG.warning("Deleting the router %s from OVN NB DB",
                                lrouter['name'])
                    txn.add(self.ovn_api.delete_lrouter(
                            utils.ovn_name(lrouter['name'])))

            for lrport_info in del_lrouter_ports_list:
                LOG.warning("Router Port found in OVN but not in "
                            "Neutron, port_id=%s", lrport_info['port'])
                if self.mode == SYNC_MODE_REPAIR:
                    LOG.warning("Deleting the port %s from OVN NB DB",
                                lrport_info['port'])
                    txn.add(self.ovn_api.delete_lrouter_port(
                            utils.ovn_lrouter_port_name(lrport_info['port']),
                            utils.ovn_name(lrport_info['lrouter']),
                            if_exists=False))
            for sroute in update_sroutes_list:
                if sroute['add']:
                    LOG.warning("Router %(id)s static routes %(route)s "
                                "found in Neutron but not in OVN",
                                {'id': sroute['id'], 'route': sroute['add']})
                    if self.mode == SYNC_MODE_REPAIR:
                        LOG.warning("Add static routes %s to OVN NB DB",
                                    sroute['add'])
                        for route in sroute['add']:
                            txn.add(self.ovn_api.add_static_route(
                                utils.ovn_name(sroute['id']),
                                ip_prefix=route['destination'],
                                nexthop=route['nexthop']))
                if sroute['del']:
                    LOG.warning("Router %(id)s static routes %(route)s "
                                "found in OVN but not in Neutron",
                                {'id': sroute['id'], 'route': sroute['del']})
                    if self.mode == SYNC_MODE_REPAIR:
                        LOG.warning("Delete static routes %s from OVN NB DB",
                                    sroute['del'])
                        for route in sroute['del']:
                            txn.add(self.ovn_api.delete_static_route(
                                utils.ovn_name(sroute['id']),
                                ip_prefix=route['destination'],
                                nexthop=route['nexthop']))
            for fip in update_fips_list:
                if fip['del']:
                    LOG.warning("Router %(id)s floating ips %(fip)s "
                                "found in OVN but not in Neutron",
                                {'id': fip['id'], 'fip': fip['del']})
                    if self.mode == SYNC_MODE_REPAIR:
                        LOG.warning(
                            "Delete floating ips %s from OVN NB DB",
                            fip['del'])
                        for nat in fip['del']:
                            txn.add(self.ovn_api.delete_nat_rule_in_lrouter(
                                utils.ovn_name(fip['id']),
                                logical_ip=nat['logical_ip'],
                                external_ip=nat['external_ip'],
                                type='dnat_and_snat'))
                if fip['add']:
                    LOG.warning("Router %(id)s floating ips %(fip)s "
                                "found in Neutron but not in OVN",
                                {'id': fip['id'], 'fip': fip['add']})
                    if self.mode == SYNC_MODE_REPAIR:
                        LOG.warning("Add floating ips %s to OVN NB DB",
                                    fip['add'])
                        for nat in fip['add']:
                            txn.add(self.ovn_api.add_nat_rule_in_lrouter(
                                utils.ovn_name(fip['id']),
                                logical_ip=nat['logical_ip'],
                                external_ip=nat['external_ip'],
                                type='dnat_and_snat'))
            for snat in update_snats_list:
                if snat['del']:
                    LOG.warning("Router %(id)s snat %(snat)s "
                                "found in OVN but not in Neutron",
                                {'id': snat['id'], 'snat': snat['del']})
                    if self.mode == SYNC_MODE_REPAIR:
                        LOG.warning("Delete snats %s from OVN NB DB",
                                    snat['del'])
                        for nat in snat['del']:
                            txn.add(self.ovn_api.delete_nat_rule_in_lrouter(
                                utils.ovn_name(snat['id']),
                                logical_ip=nat['logical_ip'],
                                external_ip=nat['external_ip'],
                                type='snat'))
                if snat['add']:
                    LOG.warning("Router %(id)s snat %(snat)s "
                                "found in Neutron but not in OVN",
                                {'id': snat['id'], 'snat': snat['add']})
                    if self.mode == SYNC_MODE_REPAIR:
                        LOG.warning("Add snats %s to OVN NB DB",
                                    snat['add'])
                        for nat in snat['add']:
                            txn.add(self.ovn_api.add_nat_rule_in_lrouter(
                                utils.ovn_name(snat['id']),
                                logical_ip=nat['logical_ip'],
                                external_ip=nat['external_ip'],
                                type='snat'))
        LOG.debug('OVN-NB Sync routers and router ports finished %s' %
                  str(datetime.now()))

    def _sync_subnet_dhcp_options(self, ctx, db_networks,
                                  ovn_subnet_dhcp_options):
        LOG.debug('OVN-NB Sync DHCP options for Neutron subnets started')

        db_subnets = {}
        filters = {'enable_dhcp': [1]}
        for subnet in self.core_plugin.get_subnets(ctx, filters=filters):
            if subnet['ip_version'] == constants.IP_VERSION_6 and (
                subnet.get('ipv6_address_mode') == constants.IPV6_SLAAC):
                continue
            db_subnets[subnet['id']] = subnet

        del_subnet_dhcp_opts_list = []
        for subnet_id, ovn_dhcp_opts in ovn_subnet_dhcp_options.items():
            if subnet_id in db_subnets:
                network = db_networks[utils.ovn_name(
                    db_subnets[subnet_id]['network_id'])]
                if constants.IP_VERSION_6 == db_subnets[subnet_id][
                        'ip_version']:
                    server_mac = ovn_dhcp_opts['options'].get('server_id')
                else:
                    server_mac = ovn_dhcp_opts['options'].get('server_mac')
                dhcp_options = self.ovn_driver.get_ovn_dhcp_options(
                    db_subnets[subnet_id], network, server_mac=server_mac)
                # Verify that the cidr and options are also in sync.
                if dhcp_options['cidr'] == ovn_dhcp_opts['cidr'] and (
                        dhcp_options['options'] == ovn_dhcp_opts['options']):
                    del db_subnets[subnet_id]
                else:
                    db_subnets[subnet_id]['ovn_dhcp_options'] = dhcp_options
            else:
                del_subnet_dhcp_opts_list.append(ovn_dhcp_opts)

        for subnet_id, subnet in db_subnets.items():
            LOG.warning('DHCP options for subnet %s is present in '
                        'Neutron but out of sync for OVN', subnet_id)
            if self.mode == SYNC_MODE_REPAIR:
                try:
                    LOG.debug('Adding/Updating DHCP options for subnet %s in '
                              ' OVN NB DB', subnet_id)
                    network = db_networks[utils.ovn_name(subnet['network_id'])]
                    # ovn_driver.add_subnet_dhcp_options_in_ovn doesn't create
                    # a new row in DHCP_Options if the row already exists.
                    # See commands.AddDHCPOptionsCommand.
                    self.ovn_driver.add_subnet_dhcp_options_in_ovn(
                        subnet, network, subnet.get('ovn_dhcp_options'))
                except RuntimeError:
                    LOG.warning('Adding/Updating DHCP options for subnet '
                                '%s failed in OVN NB DB', subnet_id)

        txn_commands = []
        for dhcp_opt in del_subnet_dhcp_opts_list:
            LOG.warning('Out of sync subnet DHCP options for subnet %s '
                        'found in OVN NB DB which needs to be deleted',
                        dhcp_opt['external_ids']['subnet_id'])
            if self.mode == SYNC_MODE_REPAIR:
                LOG.debug('Deleting subnet DHCP options for subnet %s ',
                          dhcp_opt['external_ids']['subnet_id'])
                txn_commands.append(self.ovn_api.delete_dhcp_options(
                    dhcp_opt['uuid']))

        if txn_commands:
            with self.ovn_api.transaction(check_error=True) as txn:
                for cmd in txn_commands:
                    txn.add(cmd)
        LOG.debug('OVN-NB Sync DHCP options for Neutron subnets finished')

    def _sync_port_dhcp_options(self, ctx, ports_need_sync_dhcp_opts,
                                ovn_port_dhcpv4_opts, ovn_port_dhcpv6_opts):
        LOG.debug('OVN-NB Sync DHCP options for Neutron ports with extra '
                  'dhcp options assigned started')

        txn_commands = []
        lsp_dhcp_key = {constants.IP_VERSION_4: 'dhcpv4_options',
                        constants.IP_VERSION_6: 'dhcpv6_options'}
        ovn_port_dhcp_opts = {constants.IP_VERSION_4: ovn_port_dhcpv4_opts,
                              constants.IP_VERSION_6: ovn_port_dhcpv6_opts}
        for port in ports_need_sync_dhcp_opts:
            if self.mode == SYNC_MODE_REPAIR:
                LOG.debug('Updating DHCP options for port %s in OVN NB DB',
                          port['id'])
                set_lsp = {}
                for ip_v in [constants.IP_VERSION_4, constants.IP_VERSION_6]:
                    dhcp_opts = self.ovn_driver.get_port_dhcp_options(
                        port, ip_v)
                    if not dhcp_opts or 'uuid' in dhcp_opts:
                        # If the Logical_Switch_Port.dhcpv4_options or
                        # dhcpv6_options no longer refers a port dhcp options
                        # created in DHCP_Options earlier, that port dhcp
                        # options will be deleted in the following
                        # ovn_port_dhcp_options handling.
                        set_lsp[lsp_dhcp_key[ip_v]] = (
                            dhcp_opts and [dhcp_opts['uuid']] or [])
                    else:
                        # If port has extra port dhcp options, a command will
                        # returned by ovn_driver.get_port_dhcp_options to add
                        # or update port dhcp options.
                        ovn_port_dhcp_opts[ip_v].pop(port['id'], None)
                        dhcp_options = dhcp_opts['cmd']
                        txn_commands.append(dhcp_options)
                        set_lsp[lsp_dhcp_key[ip_v]] = dhcp_options
                if set_lsp:
                    txn_commands.append(self.ovn_api.set_lswitch_port(
                        lport_name=port['id'], **set_lsp))

        for ip_v in [constants.IP_VERSION_4, constants.IP_VERSION_6]:
            for port_id, dhcp_opt in ovn_port_dhcp_opts[ip_v].items():
                LOG.warning(
                    'Out of sync port DHCPv%(ip_version)d options for '
                    '(subnet %(subnet_id)s port %(port_id)s) found in OVN '
                    'NB DB which needs to be deleted',
                    {'ip_version': ip_v,
                     'subnet_id': dhcp_opt['external_ids']['subnet_id'],
                     'port_id': port_id})

                if self.mode == SYNC_MODE_REPAIR:
                    LOG.debug('Deleting port DHCPv%d options for (subnet %s, '
                              'port %s)', ip_v,
                              dhcp_opt['external_ids']['subnet_id'], port_id)
                    txn_commands.append(self.ovn_api.delete_dhcp_options(
                        dhcp_opt['uuid']))

        if txn_commands:
            with self.ovn_api.transaction(check_error=True) as txn:
                for cmd in txn_commands:
                    txn.add(cmd)
        LOG.debug('OVN-NB Sync DHCP options for Neutron ports with extra '
                  'dhcp options assigned finished')

    def sync_networks_ports_and_dhcp_opts(self, ctx):
        LOG.debug('OVN-NB Sync networks, ports and DHCP options started')
        db_networks = {}
        for net in self.core_plugin.get_networks(ctx):
            db_networks[utils.ovn_name(net['id'])] = net

        # Ignore the floating ip ports with device_owner set to
        # constants.DEVICE_OWNER_FLOATINGIP
        db_ports = {port['id']: port for port in
                    self.core_plugin.get_ports(ctx) if not
                    port.get('device_owner', '').startswith(
                    constants.DEVICE_OWNER_FLOATINGIP)}

        ovn_all_dhcp_options = self.ovn_api.get_all_dhcp_options()
        db_network_cache = dict(db_networks)

        ports_need_sync_dhcp_opts = []
        lswitches = self.ovn_api.get_all_logical_switches_with_ports()
        del_lswitchs_list = []
        del_lports_list = []
        add_provnet_ports_list = []
        for lswitch in lswitches:
            if lswitch['name'] in db_networks:
                for lport in lswitch['ports']:
                    if lport in db_ports:
                        ports_need_sync_dhcp_opts.append(db_ports.pop(lport))
                    else:
                        del_lports_list.append({'port': lport,
                                                'lswitch': lswitch['name']})
                db_network = db_networks[lswitch['name']]
                physnet = db_network.get(pnet.PHYSICAL_NETWORK)
                # Updating provider attributes is forbidden by neutron, thus
                # we only need to consider missing provnet-ports in OVN DB.
                if physnet and not lswitch['provnet_port']:
                    add_provnet_ports_list.append(
                        {'network': db_network,
                         'lswitch': lswitch['name']})

                del db_networks[lswitch['name']]
            else:
                del_lswitchs_list.append(lswitch)

        for net_id, network in db_networks.items():
            LOG.warning("Network found in Neutron but not in "
                        "OVN DB, network_id=%s", network['id'])
            if self.mode == SYNC_MODE_REPAIR:
                try:
                    LOG.debug('Creating the network %s in OVN NB DB',
                              network['id'])
                    self._create_network_in_ovn(network)
                except RuntimeError:
                    LOG.warning("Create network in OVN NB failed for "
                                "network %s", network['id'])

        self._sync_subnet_dhcp_options(
            ctx, db_network_cache, ovn_all_dhcp_options['subnets'])

        for port_id, port in db_ports.items():
            LOG.warning("Port found in Neutron but not in OVN "
                        "DB, port_id=%s", port['id'])
            if self.mode == SYNC_MODE_REPAIR:
                try:
                    LOG.debug('Creating the port %s in OVN NB DB',
                              port['id'])
                    self._create_port_in_ovn(ctx, port)
                    if port_id in ovn_all_dhcp_options['ports_v4']:
                        _, lsp_opts = utils.get_lsp_dhcp_opts(
                            port, constants.IP_VERSION_4)
                        if lsp_opts:
                            ovn_all_dhcp_options['ports_v4'].pop(port_id)
                    if port_id in ovn_all_dhcp_options['ports_v6']:
                        _, lsp_opts = utils.get_lsp_dhcp_opts(
                            port, constants.IP_VERSION_6)
                        if lsp_opts:
                            ovn_all_dhcp_options['ports_v6'].pop(port_id)
                except RuntimeError:
                    LOG.warning("Create port in OVN NB failed for"
                                " port %s", port['id'])

        with self.ovn_api.transaction(check_error=True) as txn:
            for lswitch in del_lswitchs_list:
                LOG.warning("Network found in OVN but not in "
                            "Neutron, network_id=%s", lswitch['name'])
                if self.mode == SYNC_MODE_REPAIR:
                    LOG.debug('Deleting the network %s from OVN NB DB',
                              lswitch['name'])
                    txn.add(self.ovn_api.delete_lswitch(
                        lswitch_name=lswitch['name']))

            for provnet_port_info in add_provnet_ports_list:
                network = provnet_port_info['network']
                LOG.warning("Provider network found in Neutron but "
                            "provider network port not found in OVN DB, "
                            "network_id=%s", provnet_port_info['lswitch'])
                if self.mode == SYNC_MODE_REPAIR:
                    LOG.debug('Creating the provnet port %s in OVN NB DB',
                              utils.ovn_provnet_port_name(network['id']))
                    self.ovn_driver.create_provnet_port(
                        txn, network, network.get(pnet.PHYSICAL_NETWORK),
                        network.get(pnet.SEGMENTATION_ID))

            for lport_info in del_lports_list:
                LOG.warning("Port found in OVN but not in "
                            "Neutron, port_id=%s", lport_info['port'])
                if self.mode == SYNC_MODE_REPAIR:
                    LOG.debug('Deleting the port %s from OVN NB DB',
                              lport_info['port'])
                    txn.add(self.ovn_api.delete_lswitch_port(
                        lport_name=lport_info['port'],
                        lswitch_name=lport_info['lswitch']))
                    if lport_info['port'] in ovn_all_dhcp_options['ports_v4']:
                        LOG.debug('Deleting port DHCPv4 options for (port %s)',
                                  lport_info['port'])
                        txn.add(self.ovn_api.delete_dhcp_options(
                                ovn_all_dhcp_options['ports_v4'].pop(
                                    lport_info['port'])['uuid']))
                    if lport_info['port'] in ovn_all_dhcp_options['ports_v6']:
                        LOG.debug('Deleting port DHCPv6 options for (port %s)',
                                  lport_info['port'])
                        txn.add(self.ovn_api.delete_dhcp_options(
                                ovn_all_dhcp_options['ports_v6'].pop(
                                    lport_info['port'])['uuid']))

        self._sync_port_dhcp_options(ctx, ports_need_sync_dhcp_opts,
                                     ovn_all_dhcp_options['ports_v4'],
                                     ovn_all_dhcp_options['ports_v6'])
        LOG.debug('OVN-NB Sync networks, ports and DHCP options finished')


class OvnSbSynchronizer(OvnDbSynchronizer):
    """Synchronizer class for SB."""

    def __init__(self, core_plugin, ovn_api, ovn_driver):
        super(OvnSbSynchronizer, self).__init__(
            core_plugin, ovn_api, ovn_driver)
        self.l3_plugin = directory.get_plugin(constants.L3)

    def _sync(self):
        """Method to sync the OVN_Southbound DB with neutron DB.

        OvnSbSynchronizer will sync data from OVN_Southbound to neutron. And
        the synchronization will always be performed, no matter what mode it
        is.
        """
        # Initial delay until service is up
        greenthread.sleep(10)
        LOG.debug("Starting OVN-Southbound DB sync process")

        ctx = context.get_admin_context()
        self.sync_hostname_and_physical_networks(ctx)
        if config.is_ovn_l3():
            self.l3_plugin.schedule_unhosted_gateways()

    def sync_hostname_and_physical_networks(self, ctx):
        LOG.debug('OVN-SB Sync hostname and physical networks started')
        host_phynets_map = self.ovn_api.get_chassis_hostname_and_physnets()
        current_hosts = set(host_phynets_map)
        previous_hosts = segments_db.get_hosts_mapped_with_segments(ctx)

        stale_hosts = previous_hosts - current_hosts
        for host in stale_hosts:
            LOG.debug('Stale host %s found in Neutron, but not in OVN SB DB. '
                      'Clear its SegmentHostMapping in Neutron', host)
            self.ovn_driver.update_segment_host_mapping(host, [])

        new_hosts = current_hosts - previous_hosts
        for host in new_hosts:
            LOG.debug('New host %s found in OVN SB DB, but not in Neutron. '
                      'Add its SegmentHostMapping in Neutron', host)
            self.ovn_driver.update_segment_host_mapping(
                host, host_phynets_map[host])

        for host in current_hosts & previous_hosts:
            LOG.debug('Host %s found both in OVN SB DB and Neutron. '
                      'Trigger updating its SegmentHostMapping in Neutron, '
                      'to keep OVN SB DB and Neutron have consistent data',
                      host)
            self.ovn_driver.update_segment_host_mapping(
                host, host_phynets_map[host])

        LOG.debug('OVN-SB Sync hostname and physical networks finished')
