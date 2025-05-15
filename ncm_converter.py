import os
import json
import base64
import struct
import logging
import binascii
from glob import glob
from tqdm.auto import tqdm
from textwrap import dedent
from multiprocessing import Pool
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from datetime import datetime
from io import BytesIO
import win32pipe
import win32file
import subprocess
import threading
import sys


class TqdmLoggingHandler(logging.StreamHandler):
    """Avoid tqdm progress bar interruption by logger's output to console"""
    # see logging.StreamHandler.eval method:
    # https://github.com/python/cpython/blob/d2e2534751fd675c4d5d3adc208bf4fc984da7bf/Lib/logging/__init__.py#L1082-L1091
    # and tqdm.write method:
    # https://github.com/tqdm/tqdm/blob/f86104a1f30c38e6f80bfd8fb16d5fcde1e7749f/tqdm/std.py#L614-L620

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg, end=self.terminator)
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
handler = TqdmLoggingHandler()
fmt = '%(levelname)7s [%(asctime)s] %(message)s'
datefmt = '%Y-%m-%d %H:%M:%S'
handler.setFormatter(logging.Formatter(fmt, datefmt))
log.addHandler(handler)
DEFAULT_CHUNK_SIZE = 65536


def find_ffmpeg() -> str | None:
    paths = sys.path
    paths += os.environ['PATH'].split(';')
    for path in paths:
        if len(path) == 0:
            continue
        full_path = os.path.join(path, 'ffmpeg.exe')
        if os.path.exists(full_path):
            log.info(f'found FFMpeg in {full_path}')
            return full_path


def create_pipe(pipe_name):
    return win32pipe.CreateNamedPipe(
        pipe_name, win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
        1, DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_SIZE, 0, None  # pyright:ignore[reportArgumentType]
    )


def connect_and_write_pipe(pipe_fd, data):
    win32pipe.ConnectNamedPipe(pipe_fd, None)
    with memoryview(data) as mv:
        for i in range(0, len(data), DEFAULT_CHUNK_SIZE):
            win32file.WriteFile(pipe_fd, mv[i:i+DEFAULT_CHUNK_SIZE])
    win32file.CloseHandle(pipe_fd)


def merge_audio_with_cover(ffmpeg_path, blob_audio, blob_cover_img, output_file):
    pid = os.getpid()
    named_pipe_img = rf'\\.\pipe\ncm_conv_{pid}_img'
    named_pipe_audio = rf'\\.\pipe\ncm_conv_{pid}_audio'
    img_pipe = None
    audio_pipe = None
    try:
        img_pipe = create_pipe(named_pipe_img)
        audio_pipe = create_pipe(named_pipe_audio)
        cmd = [ffmpeg_path, '-v', 'quiet', '-y', '-i', named_pipe_img, '-i', named_pipe_audio, '-c', 'copy', '-map', '0:0', '-disposition:v', 'attached_pic', '-map', '1', '-map_metadata', '1', output_file]
        proc = subprocess.Popen(cmd)
        connect_and_write_pipe(img_pipe, blob_cover_img)
        connect_and_write_pipe(audio_pipe, blob_audio)
        assert proc.wait() == 0
    finally:
        if img_pipe:
            win32file.CloseHandle(img_pipe)
        if audio_pipe:
            win32file.CloseHandle(audio_pipe)


def dump_single_file(filepath, target_folder, after_timestamp=None, ffmpeg_path=None):
    try:
        if after_timestamp:
            creation_time = os.path.getctime(filepath)
            if creation_time < after_timestamp:
                log.info(f'Skipping "{filepath}" due to creation time before specified timestamp')
                return
        filename = os.path.basename(filepath)  # Use os.path.basename for Windows compatibility
        if not filename.endswith('.ncm'): return
        filename = filename[:-4]
        for ftype in ['mp3', 'flac']:
            fname = os.path.join(target_folder, f'{filename}.{ftype}')
            if os.path.isfile(fname):
                log.warning(f'Skipping "{filepath}" due to existing file "{fname}"')
                return

        log.info(f'Converting "{filepath}"')

        # hex to str
        core_key = binascii.a2b_hex('687A4852416D736F356B496E62617857')
        meta_key = binascii.a2b_hex('2331346C6A6B5F215C5D2630553C2728')
        unpad = lambda s: s[0:-(s[-1] if isinstance(s[-1], int) else ord(s[-1]))]
        with open(filepath, 'rb') as f:
            header = f.read(8)
            
            # str to hex
            assert binascii.b2a_hex(header) == b'4354454e4644414d'
            f.seek(2, 1)
            key_length = f.read(4)
            key_length = struct.unpack('<I', bytes(key_length))[0]
            key_data = f.read(key_length)
            key_data_array = bytearray(key_data)
            for i in range(0, len(key_data_array)):
                key_data_array[i] ^= 0x64
            key_data = bytes(key_data_array)
            cryptor = Cipher(algorithms.AES(core_key), modes.ECB(), backend=default_backend()).decryptor()
            key_data = unpad(cryptor.update(key_data) + cryptor.finalize())[17:]
            key_length = len(key_data)
            key_data = bytearray(key_data)
            key_box = bytearray(range(256))

            c = 0
            last_byte = 0
            key_offset = 0
            for i in range(256):
                swap = key_box[i]
                c = (swap + last_byte + key_data[key_offset]) & 0xff
                key_offset += 1
                if key_offset >= key_length:
                    key_offset = 0
                key_box[i] = key_box[c]
                key_box[c] = swap
                last_byte = c

            meta_length = f.read(4)
            meta_length = struct.unpack('<I', bytes(meta_length))[0]
            meta_data = f.read(meta_length)
            meta_data_array = bytearray(meta_data)
            for i in range(0, len(meta_data_array)):
                meta_data_array[i] ^= 0x63
            meta_data = bytes(meta_data_array)
            meta_data = base64.b64decode(meta_data[22:])
            cryptor = Cipher(algorithms.AES(meta_key), modes.ECB(), backend=default_backend()).decryptor()
            meta_data = unpad(cryptor.update(meta_data) + cryptor.finalize()).decode('utf-8')[6:]
            meta_data = json.loads(meta_data)

            crc32 = f.read(4)
            crc32 = struct.unpack('<I', bytes(crc32))[0]
            f.seek(5, 1)
            image_size = f.read(4)
            image_size = struct.unpack('<I', bytes(image_size))[0]
            image_data = f.read(image_size)
            target_filename = os.path.join(target_folder, f'{filename}.{meta_data["format"]}')

            with BytesIO() as m:
                chunk = bytearray()
                while True:
                    chunk = bytearray(f.read(0x8000))
                    chunk_length = len(chunk)
                    if not chunk:
                        break
                    for i in range(1, chunk_length + 1):
                        j = i & 0xff
                        chunk[i - 1] ^= key_box[(key_box[j] + key_box[(key_box[j] + j) & 0xff]) & 0xff]
                    m.write(chunk)

                audio_data = m.getvalue()
                if ffmpeg_path:
                    merge_audio_with_cover(ffmpeg_path, audio_data, image_data, target_filename)
                else:
                    with open(target_filename, 'wb') as m:
                        m.write(audio_data)
        log.info(f'Converted file saved at "{target_filename}"')
        return target_filename

    except KeyboardInterrupt:
        log.warning('Aborted')
        quit()


def list_filepaths(path):
    if os.path.isfile(path):
        return [path]
    elif os.path.isdir(path):
        return [fp for p in glob(os.path.join(path, '*')) for fp in list_filepaths(p)]
    else:
        raise ValueError(f'path not recognized: {path}')

def process_file(fp, target_folder, after_timestamp, ffmpeg_path):
    dump_single_file(fp, target_folder or os.path.dirname(fp), after_timestamp, ffmpeg_path)

def dump(*paths, n_workers=1, target_folder=None, after_timestamp=None, ffmpeg_path=None):
    header = dedent(r'''
                   _  _  ___ __  __ ___  _   _ __  __ ___
         _ __ _  _| \| |/ __|  \/  |   \| | | |  \/  | _ \
        | '_ \ || | .` | (__| |\/| | |) | |_| | |\/| |  _/
        | .__/\_, |_|\_|\___|_|  |_|___/ \___/|_|  |_|_|  
        |_|   |__/                                        
                            pyNCMDUMP                     
            https://github.com/allenfrostline/pyNCMDUMP  
    ''')
    for line in header.split('\n'):
        log.info(line)

    if ffmpeg_path is None:
        ffmpeg_path = find_ffmpeg()
    all_filepaths = [fp for p in paths for fp in list_filepaths(p)]
    if n_workers > 1:
        log.info(f'Running pyNCMDUMP with up to {n_workers} parallel workers')
        with Pool(processes=n_workers) as p:
            list(p.starmap(process_file, [(fp, target_folder, after_timestamp, ffmpeg_path) for fp in all_filepaths]))
            # list(p.map(lambda fp: process_file(fp, target_folder, after_timestamp), all_filepaths))  # Use the new function
    else:
        log.info('Running pyNCMDUMP on single-worker mode')
        for fp in tqdm(all_filepaths, leave=False):
            dump_single_file(fp, target_folder or os.path.dirname(fp), after_timestamp, ffmpeg_path)  # Use target_folder
    log.info('All finished')


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser(description='pyNCMDUMP command-line interface')
    parser.add_argument(
        'paths',
        metavar='paths',
        type=str,
        nargs='+',
        help='one or more paths to source files'
    )
    parser.add_argument(
        '-w', '--workers',
        metavar='',
        type=int,
        help=f'parallel convertion when set to more than 1 workers (default: 1)',
        default=1
    )
    parser.add_argument(
        '-t', '--target-folder',
        metavar='',
        type=str,
        help='optional target folder for converted files (default: same as source file)',
    )

    parser.add_argument(
        '-a', '--after',
        metavar='',
        type=str,
        help='optional timestamp in yymmddhhmm format to only process files created after this time',
    )

    parser.add_argument('--ffmpeg_path', type=str, default=None,
                        help='ffmpeg executable path for merging audio with embedded cover image')
    
    args = parser.parse_args()

    # Parse the after timestamp if provided
    after_timestamp = None
    if args.after:
        try:
            after_timestamp = int(datetime.strptime(args.after, '%y%m%d%H%M').timestamp())
        except ValueError:
            log.error('Invalid timestamp format. Please use yymmddhhmm format.')
            exit(1)

    args = parser.parse_args()
    dump(*args.paths, n_workers=args.workers, target_folder=args.target_folder, after_timestamp=after_timestamp, ffmpeg_path=args.ffmpeg_path)