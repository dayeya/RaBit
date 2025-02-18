import src.app_data.db_utils as db_utils
from src.download.data_structures import DownloadingPiece, FailedPiece
from src.download.piece_picker import BetterQueue, PiecePicker
from src.peer.peer_object import Peer
from src.torrent.torrent_object import Torrent

import asyncio
from hashlib import sha1
from typing import Tuple
import threading
import os
import re


def format_file_name(file_name: str) -> str:
    # remove illegal name chars
    file_name = re.sub(r'[<>:"/\\|?*]', '', file_name)
    file_name = ''.join([char for char in file_name if ord(char) > 31])
    file_name = re.sub(r'[ .]$', '', file_name)

    reserved_names = {'COM4', 'COM6', 'LPT4', 'AUX', 'LPT1', 'LPT6', 'COM1', 'NUL', 'PRN', 'COM7', 'COM5', 'COM8', 'LPT9', 'LPT3', 'LPT7', 'COM3', 'COM9', 'LPT8', 'CON', 'LPT5', 'COM2', 'LPT2'}
    if file_name.upper() in reserved_names:
        file_name += '_'

    return file_name


class File(object):
    def __init__(self, TorrentData: Torrent, piece_picker: PiecePicker, results_queue: BetterQueue, torrent_path: str, path: str, skip_hash_check: bool = False):
        self.TorrentData = TorrentData
        self.results_queue = results_queue
        self.skip_hash_check = skip_hash_check
        self.piece_picker = piece_picker
        self.torrent_path = torrent_path

        if not TorrentData.multi_file:
            self.file_names = [os.path.join(path, format_file_name(TorrentData.info[b'name'].decode('utf-8')))]
            self.file_indices = [TorrentData.length]
        else:
            path = os.path.join(path, format_file_name(TorrentData.info[b'name'].decode('utf-8')))
            os.makedirs(path, exist_ok=True)

            total = 0
            self.file_indices = []
            self.file_names = []
            for name in TorrentData.info[b'files']:
                total += name[b'length']
                self.file_indices.append(total)
                tree = name[b'path'][::-1]
                file_name = path
                while True:
                    level = tree.pop()
                    file_name = os.path.join(file_name, format_file_name(level.decode('utf-8')))
                    if not tree:
                        break
                    os.makedirs(file_name, exist_ok=True)
                self.file_names.append(file_name)

        self.fds = [os.open(file_name, os.O_RDWR | os.O_CREAT | os.O_BINARY) for file_name in self.file_names]

    def reopen_files(self):
        """
        reopens completed files in read-only mode
        :return: None
        """
        self.fds = [os.open(file_name, os.O_RDONLY | os.O_BINARY) for file_name in self.file_names]

    def close_files(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []

    def get_piece(self, piece_index: int, begin: int, length: int) -> Tuple[int, int, bytes]:
        reading_begin_index = self.TorrentData.info[b'piece length'] * piece_index + begin
        remaining_length = length
        current_piece_abs_index = reading_begin_index
        first = True
        data = b''
        for index, indice in enumerate(self.file_indices):
            if reading_begin_index >= indice:
                continue

            len_for_indice = min(remaining_length, indice - current_piece_abs_index)

            relative_file_begin = 0 if not first else current_piece_abs_index - self.file_indices[index - 1] if index > 0 else current_piece_abs_index

            os.lseek(self.fds[index], relative_file_begin, os.SEEK_SET)
            data += os.read(self.fds[index], len_for_indice)

            remaining_length -= len_for_indice
            current_piece_abs_index += len_for_indice
            first = False
            if remaining_length == 0:
                break

        if len(data) < length:  # add padding to the last piece
            data += b'\x00' * (length - len(data))

        return piece_index, begin, data

    async def save_pieces_loop(self):
        while True:
            if self.piece_picker.num_of_pieces_left == 0:
                # TODO a more elegant exit, let all interested disconnect and then switch to seeding in seeding server
                self.close_files()
                # add to completed torrents db
                db_utils.CompletedTorrentsDB().insert_torrent(PickableFile(self))
                db_utils.remove_ongoing_torrent(self.torrent_path)
                loop = asyncio.get_event_loop()
                loop.stop()

            with threading.Lock():
                piece: DownloadingPiece = await self.results_queue.get()

            # hash check
            data = piece.get_data
            piece_hash = sha1(data).digest()
            torrent_piece_hash = self.TorrentData.piece_hashes[piece.index]

            if not self.skip_hash_check:
                if piece_hash != torrent_piece_hash:
                    # TODO create corrupt pieces and blocks instances and remember the addresses of senders
                    print('received corrupted piece ', piece.index)

                    self.TorrentData.corrupted += len(data)

                    piece.previous_tries.append(FailedPiece(piece))
                    piece.reset()
                    await self.piece_picker.add_failed_piece(piece)

                    continue

            print("\033[90m{}\033[00m".format(f'got piece. {round((1 - (self.piece_picker.num_of_pieces_left - 1) / len(self.piece_picker.pieces_map)) * 100, 2)}%. have index: {piece.index}. from {len(Peer.peer_instances)} peers.'))

            # ban bad peers if any
            bad_peers = piece.get_bad_peers()
            async with asyncio.Lock():
                database = db_utils.BannedPeersDB()
                for peer_ip in bad_peers:
                    database.insert_ip(peer_ip)
                    peer = list(filter(lambda x: x.address[0] == peer_ip, Peer.peer_instances))[0]
                    peer.found_dirty = True
                    print('banned ', peer_ip)

            piece_abs_index = self.TorrentData.info[b'piece length'] * piece.index

            # save to files
            remaining_length = len(data)
            current_piece_abs_index = piece_abs_index
            piece_relative_begin = 0
            first = True
            for index, indice in enumerate(self.file_indices):
                if piece_abs_index >= indice:
                    continue

                len_for_indice = min(remaining_length, indice - current_piece_abs_index)
                piece_relative_end = piece_relative_begin + len_for_indice

                relative_file_begin = 0 if not first else current_piece_abs_index - self.file_indices[index - 1] if index > 0 else current_piece_abs_index

                os.lseek(self.fds[index], relative_file_begin, os.SEEK_SET)
                os.write(self.fds[index], data[piece_relative_begin:piece_relative_end])

                piece_relative_begin += len_for_indice
                remaining_length -= len_for_indice
                current_piece_abs_index += len_for_indice
                first = False
                if remaining_length == 0:
                    break

            self.piece_picker.num_of_pieces_left -= 1
            self.piece_picker.FILE_STATUS[piece.index] = True  # update primary bitfield
            await self.piece_picker.send_have(piece.index)
            piece.reset()

    def __del__(self):
        try:
            self.close_files()
        except (OSError, AttributeError):
            pass


class PickableFile(object):
    def __init__(self, file_object: File):
        self.info_hash = file_object.TorrentData.info_hash
        self.peer_id = file_object.TorrentData.peer_id
        self.length = file_object.TorrentData.length
        self.piece_length = file_object.TorrentData.info[b'piece length']
        self.num_pieces = len(file_object.TorrentData.piece_hashes)
        # statistics
        self.downloaded = file_object.TorrentData.downloaded
        self.uploaded = file_object.TorrentData.uploaded

        self.file_names = file_object.file_names
        self.fds = []
        self.file_indices = file_object.file_indices

        del file_object

    def reopen_files(self):
        """
        reopens completed files in read-only mode
        :return: None
        """
        self.fds = [os.open(file_name, os.O_RDONLY | os.O_BINARY) for file_name in self.file_names]

    def close_files(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []

    def get_piece(self, piece_index: int, begin: int, length: int) -> Tuple[int, int, bytes]:
        reading_begin_index = self.piece_length * piece_index + begin

        remaining_length = length
        current_piece_abs_index = reading_begin_index
        first = True
        data = b''
        for index, indice in enumerate(self.file_indices):
            if reading_begin_index >= indice:
                continue

            len_for_indice = min(remaining_length, indice - current_piece_abs_index)

            relative_file_begin = 0 if not first else current_piece_abs_index - self.file_indices[index - 1] if index > 0 else current_piece_abs_index

            os.lseek(self.fds[index], relative_file_begin, os.SEEK_SET)
            data += os.read(self.fds[index], len_for_indice)

            remaining_length -= len_for_indice
            current_piece_abs_index += len_for_indice
            first = False
            if remaining_length == 0:
                break

        if len(data) < length:  # add padding to the last piece
            data += b'\x00' * (length - len(data))

        return piece_index, begin, data

    def __del__(self):
        try:
            self.close_files()
        except OSError:
            pass
