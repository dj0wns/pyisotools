from __future__ import annotations

import json
import os
import time
from datetime import datetime
from fnmatch import fnmatch
from io import BytesIO
from pathlib import Path
from sortedcontainers import SortedDict, SortedList

from dolreader.dol import DolFile

from pyisotools.apploader import Apploader
from pyisotools.bi2 import BI2
from pyisotools.bnrparser import BNR
from pyisotools.boot import Boot
from pyisotools.fst import (FST, FSTNode, FSTRoot, InvalidEntryError,
                            InvalidFSTError)
from pyisotools.iohelper import (align_int, read_string, read_ubyte,
                                 read_uint32, write_uint32)


class FileSystemTooLargeError(Exception):
    pass


class _Progress(object):
    def __init__(self):
        self.jobProgress = 0
        self.jobSize = 0
        self._isReady = False

    def set_ready(self, ready: bool):
        self._isReady = ready

    def is_ready(self) -> bool:
        return self._isReady


class _ISOInfo(FST):

    def __init__(self, iso: Path = None):
        super().__init__()
        self.root: Path = None

        self._curEntry = 0
        self._strOfs = 0
        self._dataOfs = 0
        self._prevfile = None


class ISOBase(_ISOInfo):

    def __init__(self):
        super().__init__()
        self.progress = _Progress()

        self.isoPath = None
        self.bootheader = None
        self.bootinfo = None
        self.apploader = None
        self.dol = None
        self._rawFST = None

        self._alignmentTable = SortedDict()
        self._locationTable = SortedDict()
        self._excludeTable = SortedList()

    def _read_nodes(self, fst, node: FSTNode, strTabOfs: int) -> (FSTNode, int):
        _type = read_ubyte(fst)
        _nameOfs = int.from_bytes(fst.read(3), "big", signed=False)
        _entryOfs = read_uint32(fst)
        _size = read_uint32(fst)

        _oldpos = fst.tell()
        node.name = read_string(fst, strTabOfs + _nameOfs, encoding="shift-jis")
        fst.seek(_oldpos)

        node._id = self._curEntry

        self._curEntry += 1

        if _type == FSTNode.FOLDER:
            node.type = FSTNode.FOLDER
            node._dirparent = _entryOfs
            node._dirnext = _size

            while self._curEntry < _size:
                child = self._read_nodes(fst, FSTNode.empty(), strTabOfs)
                node.add_child(child)
        else:
            node.type = FSTNode.FILE
            node.size = _size
            node._fileoffset = _entryOfs

        return node

    def _load_from_path(self, path: Path, parentnode: FSTNode = None, ignoreList: tuple = ()):
        for entry in sorted(path.iterdir(), key=lambda x: str(x).lower()):
            disable = False
            for p in ignoreList:
                if entry.match(p):
                    disable = True

            if entry.is_file():
                child = FSTNode.file(entry.name)

                if parentnode is not None:
                    parentnode.add_child(child)

                child._alignment = self._get_alignment(child)
                child._position = self._get_location(child)
                child._exclude = disable
                child.size = entry.stat().st_size

            elif entry.is_dir():
                child = FSTNode.folder(entry.name)

                if parentnode is not None:
                    parentnode.add_child(child)

                self._load_from_path(entry, child, ignoreList=ignoreList)
            else:
                raise InvalidEntryError("Not a dir or file")

    def _init_tables(self, configPath: Path = None):
        if configPath and configPath.is_file():
            with configPath.open("r") as config:
                data = json.load(config)

            self._alignmentTable = SortedDict(data["alignment"])
            self._locationTable = SortedDict(data["location"])
            self._excludeTable = SortedList(data["exclude"])
        else:
            self._alignmentTable = SortedDict()
            self._locationTable = SortedDict()
            self._excludeTable = SortedList()

    def _recursive_extract(self, path: Path, dest: Path, iso):
        node = self.find_by_path(path)
        if not node:
            return

        if node.is_file():
            iso.seek(node._fileoffset)
            dest.write_bytes(iso.read(node.size))
            self.progress.jobProgress += node.size
        else:
            dest.mkdir(parents=True, exist_ok=True)
            for child in node.children:
                self._recursive_extract(path/child.name, dest/child.name, iso)

    def _collect_size(self, size: int) -> int:
        for node in self.children:
            if self._get_excluded(node) is True or self._get_location(node) is not None:
                continue

            if node.is_file():
                alignment = self._get_alignment(node)
                size = align_int(size, alignment)
                size += node.size
            else:
                size = node._collect_size(size)

        return align_int(size, 4)

    def _get_greatest_alignment(self) -> int:
        try:
            return self._alignmentTable.peekitem()[1]
        except IndexError:
            return 4

    def _get_alignment(self, node: [FSTNode, str]) -> int:
        if isinstance(node, FSTNode):
            _path = node.path
        else:
            _path = node

        if self._alignmentTable:
            for entry, align in self._alignmentTable.items():
                if fnmatch(_path, entry.strip()):
                    return align
        return 4

    def _get_location(self, node: [FSTNode, str]) -> int:
        if isinstance(node, FSTNode):
            _path = node.path
        else:
            _path = node

        if self._locationTable:
            return self._locationTable.get(_path)

    def _get_excluded(self, node: [FSTNode, str]) -> bool:
        if isinstance(node, FSTNode):
            _path = node.path
        else:
            _path = node

        if self._excludeTable:
            for entry in self._excludeTable:
                if fnmatch(_path, entry.strip()):
                    return True
        return False


class WiiISO(ISOBase):

    MaxSize = 4699979776

    def __init__(self):
        super().__init__()


class GamecubeISO(ISOBase):

    MaxSize = 1459978240

    def __init__(self):
        super().__init__()
        self.bnr = None

    @classmethod
    def from_root(cls, root: Path, genNewInfo: bool = False) -> GamecubeISO:
        virtualISO = cls()
        virtualISO.init_from_root(root, genNewInfo)

        if ((virtualISO.bootheader.fstOffset + virtualISO.bootheader.fstSize + 0x7FF) & -0x800) + virtualISO.datasize > virtualISO.MaxSize:
            raise FileSystemTooLargeError(
                f"{((virtualISO.bootheader.fstOffset + virtualISO.bootheader.fstSize + 0x7FF) & -0x800) + virtualISO.datasize} is larger than the max size of a GCM ({virtualISO.MaxSize})")

        if virtualISO.bootinfo.countryCode == BI2.Country.JAPAN:
            region = 2
        elif virtualISO.bootinfo.countryCode == BI2.Country.KOREA:
            region = 0
        else:
            region = virtualISO.bootinfo.countryCode - 1

        for f in virtualISO.dataPath.iterdir():
            if f.is_file() and f.match("*opening.bnr"):
                if virtualISO._get_excluded(f.name):
                    continue
                virtualISO.bnr = BNR(f, region=region)
                break

        with (virtualISO.configPath).open("r") as f:
            config = json.load(f)

        if genNewInfo and virtualISO.bnr:
            virtualISO.bnr.gameName = config["name"]
            virtualISO.bnr.gameTitle = config["name"]
            virtualISO.bnr.developerName = config["author"]
            virtualISO.bnr.developerTitle = config["author"]
            virtualISO.bnr.gameDescription = config["description"]

        return virtualISO

    @classmethod
    def from_iso(cls, iso: Path):
        virtualISO = cls()
        virtualISO.init_from_iso(iso)

        if virtualISO.bootinfo.countryCode == BI2.Country.JAPAN:
            region = BNR.Regions.JAPAN
        else:
            region = virtualISO.bootinfo.countryCode - 1

        bnrNode = None
        for child in virtualISO.children:
            if child.is_file() and fnmatch(child.path, "*opening.bnr"):
                bnrNode = child
                break

        if bnrNode:
            with iso.open("rb") as _rawISO:
                _rawISO.seek(bnrNode._fileoffset)
                virtualISO.bnr = BNR.from_data(_rawISO, region=region, size=bnrNode.size)
        else:
            virtualISO.bnr = None

        prev = FSTNode.file("fst.bin", None, virtualISO.bootheader.fstSize, virtualISO.bootheader.fstOffset)
        for node in virtualISO.nodes_by_offset():
            alignment = virtualISO._detect_alignment(node, prev)
            if alignment > 4:
                virtualISO._alignmentTable[node.path] = alignment
            prev = node

        return virtualISO

    @property
    def configPath(self) -> Path:
        if self.root:
            return self.root / "sys" / ".config.json"
        else:
            return None

    @property
    def systemPath(self) -> Path:
        if self.root:
            return self.root / "sys"
        else:
            return None

    @property
    def dataPath(self) -> Path:
        if self.root:
            return self.root / self.name
        else:
            return None

    @staticmethod
    def build_root(root: Path, dest: [Path, str] = None, genNewInfo: bool = False):
        virtualISO = GamecubeISO.from_root(root, genNewInfo)
        virtualISO.build(dest)

    @staticmethod
    def extract_from(iso: Path, dest: [Path, str] = None):
        virtualISO = GamecubeISO.from_iso(iso)
        virtualISO.extract(dest)

    def build(self, dest: [Path, str] = None, preCalc: bool = True):
        self.progress.set_ready(False)
        self.progress.jobProgress = 0
        self.progress.jobSize = self.MaxSize
        self.progress.set_ready(True)

        if dest is not None:
            fmtpath = str(dest).replace(
                r"%fullname%", f"{self.bootheader.gameName} [{self.bootheader.gameCode}{self.bootheader.makerCode}]")
            fmtpath = fmtpath.replace(r"%name%", self.bootheader.gameName)
            fmtpath = fmtpath.replace(
                r"%gameid%", f"{self.bootheader.gameCode}{self.bootheader.makerCode}")

            self.isoPath = Path(self.root / fmtpath)

        self.isoPath.parent.mkdir(parents=True, exist_ok=True)

        self.save_file_systemv((self.MaxSize - self.get_auto_blob_size())
                               & -self._get_greatest_alignment(), False, preCalc)

        self.bootheader.fstSize = len(self._rawFST.getbuffer())
        self.bootheader.fstMaxSize = self.bootheader.fstSize

        with (self.systemPath / "boot.bin").open("wb") as boot:
            self.bootheader.save(boot)

        with (self.systemPath / "bi2.bin").open("wb") as bi2:
            self.bootinfo.save(bi2)

        with (self.systemPath / "apploader.img").open("wb") as appldr:
            self.apploader.save(appldr)

        with (self.systemPath / "fst.bin").open("wb") as fst:
            fst.write(self._rawFST.getvalue())

        with self.isoPath.open("wb") as ISO:
            self.bootheader.save(ISO)
            self.progress.jobProgress += 0x440

            self.bootinfo.save(ISO)
            self.progress.jobProgress += 0x2000

            self.apploader.save(ISO)
            self.progress.jobProgress += self.apploader.loaderSize + self.apploader.trailerSize

            ISO.write(b"\x00" * (self.bootheader.dolOffset - ISO.tell()))
            self.dol.save(ISO, self.bootheader.dolOffset)
            self.progress.jobProgress += self.dol.size

            ISO.seek(ISO.tell() + self.dol.size)
            ISO.write(b"\x00" * (self.bootheader.fstOffset - ISO.tell()))
            ISO.write(self._rawFST.getvalue())
            self.progress.jobProgress += len(self._rawFST.getbuffer())

            for child in self.rfiles:
                if child.is_file() and not self._get_excluded(child):
                    ISO.write(b"\x00" * (child._fileoffset - ISO.tell()))
                    ISO.seek(child._fileoffset)
                    ISO.write((self.root / self.name /
                               child.path).read_bytes())
                    ISO.seek(0, 2)
                    self.progress.jobProgress += child.size

            ISO.write(b"\x00" * (self.MaxSize - ISO.tell()))
            self.progress.jobProgress = self.MaxSize

    def extract(self, dest: [Path, str] = None):
        self.progress.set_ready(False)
        self.progress.jobProgress = 0

        jobSize = self.size + \
            (0x2440 + (self.apploader.loaderSize + self.apploader.trailerSize))
        jobSize += self.dol.size
        jobSize += len(self._rawFST.getbuffer())

        self.progress.jobSize = jobSize
        self.progress.set_ready(True)

        if dest is not None:
            self.root = Path(f"{dest}/root")

        systemPath = self.systemPath
        self.root.mkdir(parents=True, exist_ok=True)
        systemPath.mkdir(exist_ok=True)

        with (systemPath / "boot.bin").open("wb") as f:
            self.bootheader.save(f)

        self.progress.jobProgress += 0x440

        with (systemPath / "bi2.bin").open("wb") as f:
            self.bootinfo.save(f)

        self.progress.jobProgress += 0x2000

        with (systemPath / "apploader.img").open("wb") as f:
            self.apploader.save(f)

        self.progress.jobProgress += self.apploader.loaderSize + self.apploader.trailerSize

        with (systemPath / "main.dol").open("wb") as f:
            self.dol.save(f)

        self.progress.jobProgress += self.dol.size

        with (systemPath / "fst.bin").open("wb") as f:
            f.write(self._rawFST.getvalue())

        self.progress.jobProgress += len(self._rawFST.getbuffer())
        self.save_config()

        self.dataPath.mkdir(parents=True, exist_ok=True)
        with self.isoPath.open("rb") as _iso:
            for child in self.rchildren:
                _dest = self.dataPath / child.path
                if child.is_file():
                    with _dest.open("wb") as f:
                        _iso.seek(child._fileoffset)
                        f.write(_iso.read(child.size))
                        self.progress.jobProgress += child.size
                else:
                    _dest.mkdir(exist_ok=True)

        self.progress.jobProgress = self.progress.jobSize

    def extract_system_data(self, dest: [Path, str] = None):
        self.progress.set_ready(False)
        self.progress.jobProgress = 0

        jobSize = 0x2440 + (self.apploader.loaderSize +
                            self.apploader.trailerSize)
        jobSize += self.dol.size
        jobSize += len(self._rawFST.getbuffer())

        self.progress.jobSize = jobSize
        self.progress.set_ready(True)

        systemPath = dest / "sys"
        systemPath.mkdir(parents=True, exist_ok=True)

        with (systemPath / "boot.bin").open("wb") as f:
            self.bootheader.save(f)

        self.progress.jobProgress += 0x440

        with (systemPath / "bi2.bin").open("wb") as f:
            self.bootinfo.save(f)

        self.progress.jobProgress += 0x2000

        with (systemPath / "apploader.img").open("wb") as f:
            self.apploader.save(f)

        self.progress.jobProgress += self.apploader.loaderSize + self.apploader.trailerSize

        with (systemPath / "main.dol").open("wb") as f:
            self.dol.save(f)

        self.progress.jobProgress += self.dol.size

        with (systemPath / "fst.bin").open("wb") as f:
            f.write(self._rawFST.getvalue())

        self.progress.jobProgress += len(self._rawFST.getbuffer())

    def save_system_data(self):
        self.progress.set_ready(False)
        self.progress.jobProgress = 0

        jobSize = 0x2440 + (self.apploader.loaderSize + self.apploader.trailerSize)
        jobSize += self.dol.size
        jobSize += self.num_children()

        self.progress.jobSize = jobSize
        self.progress.set_ready(True)

        systemPath = self.root / "sys"

        with (systemPath / "boot.bin").open("wb") as f:
            self.bootheader.save(f)

        self.progress.jobProgress += 0x440

        with (systemPath / "bi2.bin").open("wb") as f:
            self.bootinfo.save(f)

        self.progress.jobProgress += 0x2000

        with (systemPath / "apploader.img").open("wb") as f:
            self.apploader.save(f)

        self.progress.jobProgress += self.apploader.loaderSize + self.apploader.trailerSize

        with (systemPath / "main.dol").open("wb") as f:
            self.dol.save(f)

        self.progress.jobProgress += self.dol.size
        self._save_config_regen()
        self.progress.jobProgress = self.progress.jobSize

    def save_system_datav(self):
        self.progress.set_ready(False)
        self.progress.jobProgress = 0

        jobSize = 0x2440 + (self.apploader.loaderSize + self.apploader.trailerSize)
        jobSize += self.dol.size

        self.progress.jobSize = jobSize
        self.progress.set_ready(True)
        
        with self.isoPath.open("r+b") as ISO:
            self.bootheader.save(ISO)
            self.progress.jobProgress += 0x440

            self.bootinfo.save(ISO)
            self.progress.jobProgress += 0x2000

            self.apploader.save(ISO)

            ISO.write(b"\x00" * (self.bootheader.dolOffset - ISO.tell()))
            self.dol.save(ISO, self.bootheader.dolOffset)

            self.progress.jobProgress += self.dol.size

    def get_auto_blob_size(self) -> int:
        def _collect_size(node: FSTNode, _size: int):
            for child in node.children:
                if child._exclude or child._position:
                    continue

                if child.is_file():
                    _size = align_int(_size, child._alignment) + child.size
                else:
                    _size = _collect_size(child, _size)

            return _size

        return _collect_size(self, 0)

    def init_from_iso(self, iso: Path):
        self.isoPath = iso
        self.root = Path(iso.parent / "root").resolve()
        with iso.open("rb") as _rawISO:
            _rawISO.seek(0)
            self.bootheader = Boot(_rawISO)
            self.bootinfo = BI2(_rawISO)
            self.apploader = Apploader(_rawISO)
            self.dol = DolFile(_rawISO, startpos=self.bootheader.dolOffset)
            _rawISO.seek(self.bootheader.fstOffset)
            self._rawFST = BytesIO(_rawISO.read(self.bootheader.fstSize))

        self._rawFST.seek(0)
        self.load_file_systemv(self._rawFST)

    def init_from_root(self, root: Path, genNewInfo: bool = False):
        self.root = root

        with (self.root / "sys" / "main.dol").open("rb") as _dol:
            self.dol = DolFile(_dol)

        with (self.root / "sys" / "boot.bin").open("rb") as f:
            self.bootheader = Boot(f)

        with (self.root / "sys" / "bi2.bin").open("rb") as f:
            self.bootinfo = BI2(f)

        with (self.root / "sys" / "apploader.img").open("rb") as f:
            self.apploader = Apploader(f)

        self._init_tables(self.configPath)
        with self.configPath.open("r") as f:
            config = json.load(f)

        if genNewInfo:
            self.bootheader.gameName = config["name"]
            self.bootheader.gameCode = config["gameid"][:4]
            self.bootheader.makerCode = config["gameid"][4:6]
            self.bootheader.version = int(config["version"])
            self.apploader.buildDate = datetime.today().strftime("%Y/%m/%d")

        self.isoPath = Path(
            root.parent / f"{self.bootheader.gameName} [{self.bootheader.gameCode}{self.bootheader.makerCode}].iso").resolve()

        self.bootheader.dolOffset = (
            0x2440 + self.apploader.trailerSize + 0x1FFF) & -0x2000
        self.bootheader.fstOffset = (
            self.bootheader.dolOffset + self.dol.size + 0x7FF) & -0x800

        self._rawFST = BytesIO()
        self.load_file_system(self.root / self.name,
                              self, ignoreList=[])

        self.bootheader.fstSize = len(self._rawFST.getbuffer())
        self.bootheader.fstMaxSize = self.bootheader.fstSize

    def extract_path(self, path: Path, dest: Path):
        self.progress.set_ready(False)

        node = self.find_by_path(path)

        if not node:
            return

        self.progress.jobProgress = 0
        self.progress.jobSize = node.datasize
        self.progress.set_ready(True)

        with self.isoPath.open("rb") as _rawISO:
            self._recursive_extract(path, dest / node.name, _rawISO)

        self.progress.jobProgress = self.progress.jobSize

    def replace_path(self, path: str, new: Path):
        """
        Replaces the node that matches `path` with the data at path `new`

            path: Virtual path to node to replace
            new:  Path to file/folder to replace with
        """
        if not new.exists():
            return

        newNode = self.from_path(new)
        oldNode = self.find_by_path(path)

        oldNode.parent.add_child(newNode)
        oldNode.destroy()

    ## FST HANDLING ##

    def pre_calc_metadata(self, startpos: int):
        """
        Pre calculates all node offsets for viewing the node locations before compile

        The results of this function are only valid until the FST is changed in
        a way that impacts file offsets
        """
        _dataOfs = align_int(startpos, 4)
        _curEntry = 1
        for child in self.rchildren:
            if child.is_file() and child._position:
                child._fileoffset = align_int(child._position, child._alignment)

            if child._exclude:
                if child.is_file():
                    child._fileoffset = 0
                continue

            child._id = _curEntry
            _curEntry += 1

            if child.is_file():
                child._fileoffset = align_int(_dataOfs, child._alignment)
                _dataOfs += child.size
            else:
                child._dirparent = child.parent._id
                child._dirnext = child.size + child._id

    def load_file_system(self, path: Path, parentnode: FSTNode = None, ignoreList=[]):
        """
        Converts a directory into an FST and loads into self for further use

            path:       Path to input directory
            parentnode: Parent to store all info under
            ignorelist: List of filepaths to ignore as glob patterns
        """

        self._init_tables(self.configPath)

        if len(self._excludeTable) > 0:
            ignoreList.extend(self._excludeTable)

        self._load_from_path(path, parentnode, ignoreList)
        self.pre_calc_metadata(self.MaxSize - self.get_auto_blob_size())

    def load_file_systemv(self, fst):
        """
        Loads the file system data from a memory buffer into self for further use

            fst: BytesIO or opened file object containing the FST of an ISO
        """

        if fst.read(1) != b"\x01":
            raise InvalidFSTError("Invalid Root flag found")
        elif fst.read(3) != b"\x00\x00\x00":
            raise InvalidFSTError("Invalid Root string offset found")
        elif fst.read(4) != b"\x00\x00\x00\x00":
            raise InvalidFSTError("Invalid Root offset found")

        self._alignmentTable = SortedDict()
        entryCount = read_uint32(fst)

        self._curEntry = 1
        while self._curEntry < entryCount:
            child = self._read_nodes(fst, FSTNode.empty(), entryCount * 0xC)
            self.add_child(child)

    def save_file_systemv(self, startpos: int = 0, useConfig: bool = True, preCalc: bool = True):
        """
        Save the file system data to the target ISO

            fst:       BytesIO or opened file object to write fst data to
            startpos:  Starting position in ISO to write files
            useConfig: Initialize node info using the root config
        """

        if useConfig:
            self._init_tables(self.configPath)
            self.pre_calc_metadata(self.MaxSize - self.get_auto_blob_size())
        elif preCalc:
            self.pre_calc_metadata(self.MaxSize - self.get_auto_blob_size())

        self._rawFST.seek(0)
        self._rawFST.write(b"\x01\x00\x00\x00\x00\x00\x00\x00")
        write_uint32(self._rawFST, len(self))

        _curEntry = 1
        _strOfs = 0
        _strTableOfs = self.strTableOfs

        for child in self.rchildren:
            if child._exclude:
                continue

            child._id = _curEntry

            self._rawFST.write(b"\x01" if child.is_dir() else b"\x00")
            self._rawFST.write((_strOfs).to_bytes(3, "big", signed=False))
            write_uint32(self._rawFST, child.parent._id if child.is_dir() else child._fileoffset)
            write_uint32(self._rawFST, len(child) +
                        _curEntry if child.is_dir() else child.size)

            _curEntry += 1

            _oldpos = self._rawFST.tell()
            self._rawFST.seek(_strOfs + _strTableOfs)
            self._rawFST.write(child.name.encode("shift-jis") + b"\x00")
            _strOfs += len(child.name) + 1

            self._rawFST.seek(_oldpos)

    def load_config(self, path: Path = None):
        self._init_tables(path)

    def save_config(self):
        config = {"name": self.bootheader.gameName,
                  "gameid": self.bootheader.gameCode + self.bootheader.makerCode,
                  "version": self.bootheader.version,
                  "author": self.bnr.developerTitle if self.bnr else "",
                  "description": self.bnr.gameDescription if self.bnr else "",
                  "alignment": self._alignmentTable,
                  "location": self._locationTable,
                  "exclude": [x for x in self._excludeTable]}

        with self.configPath.open("w") as f:
            json.dump(config, f, indent=4)

    def _save_config_regen(self):
        self._alignmentTable.clear()
        self._locationTable.clear()
        self._excludeTable.clear()

        for node in self.rchildren:
            if node.is_file():
                if node._alignment > 4:
                    self._alignmentTable[node.path] = node._alignment
                if node._position:
                    self._locationTable[node.path] = node._position
            if node._exclude:
                self._excludeTable.add(node.path)

        self.save_config()
