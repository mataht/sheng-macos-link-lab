"""Boundary tests for the narrow Mach-O symbol rewrite, without Apple tools."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("link_probe", Path(__file__).parents[1] / "scripts" / "link_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def object_bytes(strings=b"\0_kmod_info\0", symbols=((1, 0x0F),)):
    symoff = 56
    stroff = symoff + 16 * len(symbols)
    header = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0xC0000002, 1, 1, 24, 0, 0)
    command = struct.pack("<6I", 2, 24, symoff, len(symbols), stroff, len(strings))
    entries = b"".join(struct.pack("<IBBHQ", index, typ, 1, 0, 0) for index, typ in symbols)
    return header + command + entries + strings


class SymbolRewriteTests(unittest.TestCase):
    def test_only_target_spelling_changes(self):
        original = object_bytes()
        edited, record = probe.rename_kmod(original)
        offset = record["offset"]
        self.assertEqual(len(original), len(edited))
        self.assertEqual(original[:offset], edited[:offset])
        self.assertEqual(original[offset + 10:], edited[offset + 10:])
        self.assertEqual(edited[offset:offset + 10], b"_ufs_kmod_")

    def test_shared_suffix_rejected(self):
        with self.assertRaisesRegex(ValueError, "shared symbol string"):
            probe.rename_kmod(object_bytes(symbols=((1, 0x0F), (6, 1))))

    def test_shared_prefix_rejected(self):
        with self.assertRaisesRegex(ValueError, "shared symbol string"):
            probe.rename_kmod(object_bytes(b"\0prefix_kmod_info\0", ((7, 0x0F), (1, 1))))

    def test_duplicate_definition_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            probe.rename_kmod(object_bytes(symbols=((1, 0x0F), (1, 0x0F))))

    def test_stab_undefined_and_local_rejected(self):
        for typ in (0xEF, 1, 0x0E):
            with self.subTest(typ=typ), self.assertRaisesRegex(ValueError, "external defined"):
                probe.rename_kmod(object_bytes(symbols=((1, typ),)))

    def test_wrong_arm64e_subtype_rejected(self):
        data = bytearray(object_bytes())
        struct.pack_into("<I", data, 8, 2)
        with self.assertRaisesRegex(ValueError, "unexpected Mach-O header"):
            probe.rename_kmod(bytes(data))

    def test_table_overlap_rejected(self):
        data = bytearray(object_bytes())
        struct.pack_into("<I", data, 48, 56)
        with self.assertRaisesRegex(ValueError, "tables overlap"):
            probe.rename_kmod(bytes(data))

    def test_wrong_archive_hash_rejected_before_extraction(self):
        root = Path(__file__).resolve().parents[1]
        scratch = root / "scratch" / "test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        scratch.resolve().relative_to(root)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            Path(folder).resolve().relative_to(root)
            path = Path(folder) / "bad.zip"
            path.write_bytes(b"not the pinned artifact")
            with self.assertRaisesRegex(ValueError, "artifact SHA256 mismatch"):
                probe.prepare(path, Path(folder) / "output")
            self.assertFalse((Path(folder) / "output").exists())


if __name__ == "__main__":
    unittest.main()
