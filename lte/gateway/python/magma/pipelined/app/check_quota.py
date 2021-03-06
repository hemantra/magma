"""
Copyright (c) Facebook, Inc. and its affiliates.
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree.
"""
import netifaces
import ipaddress
from typing import NamedTuple, Dict, List

from ryu.lib import hub
from ryu.lib.packet import ether_types
from ryu.ofproto.inet import IPPROTO_TCP
from ryu.controller.controller import Datapath
from ryu.ofproto.ofproto_v1_4 import OFPP_LOCAL
from ryu.ofproto.ofproto_v1_4_parser import OFPFlowStats

from lte.protos.pipelined_pb2 import SubscriberQuotaUpdate, \
    ActivateFlowsRequest, SetupFlowsResult
from magma.pipelined.app.base import MagmaController, ControllerType
from magma.pipelined.app.inout import INGRESS, EGRESS
from magma.pipelined.imsi import encode_imsi
from magma.pipelined.openflow import flows
from magma.pipelined.directoryd_client import get_record
from magma.pipelined.openflow.magma_match import MagmaMatch
from magma.pipelined.openflow.registers import Direction, IMSI_REG, \
    DIRECTION_REG


class CheckQuotaController(MagmaController):
    """
    Quota Check Controller

    This controller recognizes special IP addr that IMSI sends a request to and
    routes that request to a flask server to check user quota.
    """

    APP_NAME = "check_quota"
    APP_TYPE = ControllerType.LOGICAL
    CheckQuotaConfig = NamedTuple(
        'CheckQuotaConfig',
        [('bridge_ip', str), ('quota_check_ip', str),
         ('has_quota_port', int), ('no_quota_port', int),
         ('cwf_bridge_mac', str)],
    )

    def __init__(self, *args, **kwargs):
        super(CheckQuotaController, self).__init__(*args, **kwargs)
        self.config = self._get_config(kwargs['config'])
        self.tbl_num = self._service_manager.get_table_num(self.APP_NAME)
        self.next_main_table = self._service_manager.get_next_table_num(
            self.APP_NAME)
        self.next_table = \
            self._service_manager.get_table_num(INGRESS)
        self.egress_table = self._service_manager.get_table_num(EGRESS)
        self.fake_ip_network = ipaddress.ip_network('192.168.0.0/16')
        self.fake_ip_iterator = self.fake_ip_network.hosts()
        self.arpd_controller_fut = kwargs['app_futures']['arpd']
        self.arp_contoller = None
        self.ip_rewrite_scratch = \
            self._service_manager.allocate_scratch_tables(self.APP_NAME, 1)[0]
        self._clean_restart = kwargs['config']['clean_restart']
        self._datapath = None

    def _get_config(self, config_dict: Dict) -> NamedTuple:
        def get_virtual_iface_mac(iface):
            virt_ifaddresses = netifaces.ifaddresses(iface)
            return virt_ifaddresses[netifaces.AF_LINK][0]['addr']

        return self.CheckQuotaConfig(
            bridge_ip=config_dict['bridge_ip_address'],
            quota_check_ip=config_dict['quota_check_ip'],
            has_quota_port=config_dict['has_quota_port'],
            no_quota_port=config_dict['no_quota_port'],
            cwf_bridge_mac=get_virtual_iface_mac(config_dict['bridge_name']),
        )

    # pylint:disable=unused-argument
    def setup(self, requests: List[ActivateFlowsRequest],
              quota_updates: List[SubscriberQuotaUpdate],
              startup_flows: List[OFPFlowStats]) -> SetupFlowsResult:
        """
        Setup current check quota flows.
        """
        # TODO Potentially we can run a diff logic but I don't think there is
        # benefit(we don't need stats here)
        self._delete_all_flows(self._datapath)
        self.update_subscriber_quota_state(quota_updates)
        self._install_default_flows(self._datapath)

        return SetupFlowsResult.SUCCESS

    def initialize_on_connect(self, datapath: Datapath):
        self._datapath = datapath
        self._delete_all_flows(datapath)
        self._install_default_flows(datapath)

    def cleanup_on_disconnect(self, datapath: Datapath):
        self._delete_all_flows(datapath)

    def update_subscriber_quota_state(self,
                                      updates: List[SubscriberQuotaUpdate]):
        for update in updates:
            imsi = update.sid.id
            if update.update_type == SubscriberQuotaUpdate.VALID_QUOTA:
                self._add_subscriber_flow(imsi, update.mac_addr, True)
            elif update.update_type == SubscriberQuotaUpdate.NO_QUOTA:
                self._add_subscriber_flow(imsi, update.mac_addr, False)
            elif update.update_type == SubscriberQuotaUpdate.TERMINATE:
                self._remove_subscriber_flow(imsi)

    def _add_subscriber_flow(self, imsi: str, ue_mac: str, has_quota: bool):
        """
        Redirect the UE flow to the dedicated flask server.
        On return traffic rewrite the IP/port so the redirection is seamless.

        Match incoming user traffic:
            1. Rewrite ip src to be in same subnet as check quota server
            2. Rewrite ip dst to check quota server
            3. Rewrite eth dst to check quota server
            4. Rewrite tcp dst port to either quota/non quota

            5. LEARN action
                This will rewrite the ip src and dst and tcp port for traffic
                coming back to the UE

            6. ARP controller arp clamp
                Sets the ARP clamping(for ARPs from the check quota server)
                for the fake IP we used to reach the check quota server

        """
        parser = self._datapath.ofproto_parser
        fake_ip = self._next_fake_ip()

        if has_quota:
            tcp_dst = self.config.has_quota_port
        else:
            tcp_dst = self.config.no_quota_port
        match = MagmaMatch(
            imsi=encode_imsi(imsi), eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=IPPROTO_TCP, direction=Direction.OUT,
            vlan_vid=(0x1000, 0x1000),
            ipv4_dst=self.config.quota_check_ip
        )
        actions = [
            parser.NXActionLearn(
                table_id=self.ip_rewrite_scratch,
                priority=flows.UE_FLOW_PRIORITY,
                specs=[
                    parser.NXFlowSpecMatch(
                        src=ether_types.ETH_TYPE_IP, dst=('eth_type_nxm', 0),
                        n_bits=16
                    ),
                    parser.NXFlowSpecMatch(
                        src=IPPROTO_TCP, dst=('ip_proto_nxm', 0), n_bits=8
                    ),
                    parser.NXFlowSpecMatch(
                        src=Direction.IN,
                        dst=(DIRECTION_REG, 0),
                        n_bits=32
                    ),
                    parser.NXFlowSpecMatch(
                        src=int(ipaddress.IPv4Address(self.config.bridge_ip)),
                        dst=('ipv4_src_nxm', 0),
                        n_bits=32
                    ),
                    parser.NXFlowSpecMatch(
                        src=int(fake_ip),
                        dst=('ipv4_dst_nxm', 0),
                        n_bits=32
                    ),
                    parser.NXFlowSpecMatch(
                        src=('tcp_src_nxm', 0),
                        dst=('tcp_dst_nxm', 0),
                        n_bits=16
                    ),
                    parser.NXFlowSpecMatch(
                        src=tcp_dst,
                        dst=('tcp_src_nxm', 0),
                        n_bits=16
                    ),
                    parser.NXFlowSpecMatch(
                        src=encode_imsi(imsi),
                        dst=(IMSI_REG, 0),
                        n_bits=64
                    ),
                    parser.NXFlowSpecLoad(
                        src=('ipv4_src_nxm', 0),
                        dst=('ipv4_dst_nxm', 0),
                        n_bits=32
                    ),
                    parser.NXFlowSpecLoad(
                        src=int(
                            ipaddress.IPv4Address(self.config.quota_check_ip)),
                        dst=('ipv4_src_nxm', 0),
                        n_bits=32
                    ),
                    parser.NXFlowSpecLoad(
                        src=80,
                        dst=('tcp_src_nxm', 0),
                        n_bits=16
                    ),
                ]
            ),
            parser.OFPActionSetField(ipv4_src=str(fake_ip)),
            parser.OFPActionSetField(ipv4_dst=self.config.bridge_ip),
            parser.OFPActionSetField(eth_dst=self.config.cwf_bridge_mac),
            parser.OFPActionSetField(tcp_dst=tcp_dst),
            parser.OFPActionPopVlan()
        ]
        flows.add_output_flow(
            self._datapath, self.tbl_num, match, actions,
            priority=flows.UE_FLOW_PRIORITY,
            output_port=OFPP_LOCAL)

        # For traffic from the check quota server rewrite src ip and port
        match = MagmaMatch(
            imsi=encode_imsi(imsi), eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=IPPROTO_TCP, direction=Direction.IN,
            ipv4_src=self.config.bridge_ip)
        actions = [
            parser.NXActionResubmitTable(table_id=self.ip_rewrite_scratch)]
        flows.add_resubmit_next_service_flow(
            self._datapath, self.tbl_num, match, actions,
            priority=flows.DEFAULT_PRIORITY,
            resubmit_table=self.egress_table
        )
        hub.spawn(self._setup_fake_ip_arp, str(imsi), fake_ip)

    def _setup_fake_ip_arp(self, imsi, fake_ip, retries=10):
        for _ in range(0, retries):
            mac = get_record(imsi, 'mac_addr')
            if mac is not None:
                break
            self.logger.debug("Mac not found for subscriber %s, retrying", imsi)
            hub.sleep(0.1)

        if mac is None:
            self.logger.error("Mac for subscriber %s, not found after %d"
                              "retries, exiting.", imsi, retries)
            return

        self.logger.info("Received mac %s for subscriber %s, with fake ip %s",
                          mac, imsi, fake_ip)

        if self.arp_contoller or self.arpd_controller_fut.done():
            if not self.arp_contoller:
                self.arp_contoller = self.arpd_controller_fut.result()
            self.arp_contoller.set_incoming_arp_flows(self._datapath, fake_ip,
                                                      mac)

    def _remove_subscriber_flow(self, imsi: str):
        match = MagmaMatch(
            imsi=encode_imsi(imsi), eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=IPPROTO_TCP, direction=Direction.OUT,
            ipv4_dst=self.config.quota_check_ip
        )
        flows.delete_flow(self._datapath, self.tbl_num, match)

        match = MagmaMatch(
            imsi=encode_imsi(imsi), eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=IPPROTO_TCP, direction=Direction.IN,
            ipv4_src=self.config.bridge_ip)
        flows.delete_flow(self._datapath, self.tbl_num, match)

        match = MagmaMatch(
            imsi=encode_imsi(imsi), eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=IPPROTO_TCP, direction=Direction.OUT,
            vlan_vid=(0x1000, 0x1000),
            ipv4_dst=self.config.quota_check_ip
        )
        flows.delete_flow(self._datapath, self.tbl_num, match)

    def _install_default_flows(self, datapath: Datapath):
        """
        Set the default flows to just forward to next app.

        Args:
            datapath: ryu datapath struct
        """
        # Default flows for non matched traffic
        inbound_match = MagmaMatch(direction=Direction.IN)
        outbound_match = MagmaMatch(direction=Direction.OUT)
        flows.add_resubmit_next_service_flow(
            datapath, self.tbl_num, inbound_match, [],
            priority=flows.MINIMUM_PRIORITY,
            resubmit_table=self.next_main_table)
        flows.add_resubmit_next_service_flow(
            datapath, self.tbl_num, outbound_match, [],
            priority=flows.MINIMUM_PRIORITY,
            resubmit_table=self.next_main_table)

    def _delete_all_flows(self, datapath: Datapath):
        flows.delete_all_flows_from_table(datapath, self.tbl_num)
        flows.delete_all_flows_from_table(datapath, self.ip_rewrite_scratch)

    def _next_fake_ip(self):
        ip = next(self.fake_ip_iterator, None)
        while not self._is_fake_ip_valid(ip):
            ip = next(self.fake_ip_iterator, None)
            if ip is None:
                self.fake_ip_iterator = self.fake_ip_network.hosts()
        return ip

    def _is_fake_ip_valid(self, ip_addr):
        if ip_addr is None or str(ip_addr) == self.config.bridge_ip or \
                str(ip_addr) == self.config.quota_check_ip:
            return False
        return True
