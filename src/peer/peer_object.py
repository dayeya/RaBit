from src.torrent.torrent_object import Torrent
import src.app_data.db_utils as db_utils

import time
from typing import Tuple, List, Set
import bitstring


class Peer(object):
    """
    object to store attributes of a peer and some stats
    """
    peer_instances: List = []
    MAX_ENDGAME_REQUESTS = 5

    def __init__(self, writer, TorrentData: Torrent, address: Tuple[str, int], geodata: Tuple[str, str, float, float]):
        self.writer = writer

        self.torrent = TorrentData

        self.MAX_PIPELINE_SIZE = 10  # 10 is default
        self.address = address

        self.is_chocked = True  # am I chocked?
        self.is_interested = True  # am I interested in what the peer offers? always true on download
        self.am_chocked = True  # have I chocked the peer?
        self.am_interested = False  # is the peer interested in what I offer?

        self.have_pieces = bitstring.BitArray(bin='0' * len(self.torrent.piece_hashes))
        self.is_seed = False
        self.pipelined_requests: Set = set()
        self.control_msg_queue: List[bytes] = []

        self.endgame_cancel_msg_sent: Set = set()  # blocks I already sent Cancel to
        self.endgame_request_msg_sent: Set = set()  # blocks I requested
        self.endgame_blocks: Set = set()  # blocks available to request
        self.is_in_endgame = False

        self.last_data_sent = time.time()

        self.download_rate = 0  # in KiB/s
        self.upload_rate = 0  # in KiB/s
        self.downloaded = 0  # in bytes
        self.uploaded = 0  # in bytes

        self.geodata = geodata
        self.peer_id = None
        self.client = None
        self.found_dirty = False

    def add_peer_id(self, peer_id: bytes):
        self.peer_id = peer_id
        self.client = db_utils.get_client(peer_id)
        Peer.peer_instances.append(self)

    def update_upload_rate(self, len_bytes_sent: int):
        # TODO make real upload rate using a counter
        # idk how but this function generates ridiculously incredible downloading on account of cpu usage
        # and breaks when working with real download rate (not len)
        rn = time.time()
        if (dt := rn - self.last_data_sent) < 0.05:
            return
        rate = (len_bytes_sent / 1024) / dt

        if self.is_in_endgame:
            self.MAX_PIPELINE_SIZE = min(rate + 2, Peer.MAX_ENDGAME_REQUESTS)
        else:
            # update pipeline size using rtorrent algorithm
            if rate < 20:
                self.MAX_PIPELINE_SIZE = rate + 2
            else:
                self.MAX_PIPELINE_SIZE = rate / 5 + 18

        self.last_data_sent = rn
        self.upload_rate = rate

    def __repr__(self):
        return f"peer id: {self.peer_id}, address: {self.address}, geodata: {self.geodata}"

    def __hash__(self):
        return hash(repr(self))
