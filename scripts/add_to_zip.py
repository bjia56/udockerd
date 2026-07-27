#!/usr/bin/env python3
"""
Usage:
    python add_to_zip.py /path/to/source /path/to/archive.zip target/path/inside/zip [--dry-run]

This appends the source (file or directory) into the ZIP under the given target path.
If source is a file, it's written as a file in the zip.
If source is a directory, its contents are added under the target prefix.
"""
import os
import sys
import zipfile


def add_to_zip(source, zip_path, target_path, dry_run=False):
    source = os.path.abspath(source)

    if os.path.isfile(source):
        target_path = target_path.strip("/\\").replace(os.path.sep, "/")

        if dry_run:
            print(f"[DRY RUN] Would add file {source} to {zip_path} as '{target_path}'")
        else:
            with zipfile.ZipFile(zip_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(source, target_path)

    elif os.path.isdir(source):
        target_prefix = target_path.strip("/\\")
        target_prefix = target_prefix.rstrip("/") + "/" if target_prefix else ""

        if dry_run:
            print(
                f"[DRY RUN] Would add contents of {source} to {zip_path} "
                f"under prefix '{target_prefix}'"
            )
            for root, dirs, files in os.walk(source):
                rel_root = os.path.relpath(root, source)
                rel_root = "" if rel_root == "." else rel_root.replace(os.path.sep, "/") + "/"

                if not files and not dirs:
                    arcname = target_prefix + rel_root
                    print(f"[DRY RUN]   Would add empty directory: {arcname}")

                for f in files:
                    arcname = (target_prefix + rel_root + f).replace(os.path.sep, "/")
                    print(f"[DRY RUN]   Would add: {arcname}")
        else:
            with zipfile.ZipFile(zip_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(source):
                    rel_root = os.path.relpath(root, source)
                    rel_root = "" if rel_root == "." else rel_root.replace(os.path.sep, "/") + "/"

                    if not files and not dirs:
                        zf.writestr(target_prefix + rel_root, "")

                    for f in files:
                        full = os.path.join(root, f)
                        arcname = (target_prefix + rel_root + f).replace(os.path.sep, "/")
                        zf.write(full, arcname)
    else:
        raise ValueError(f"Source path does not exist or is not a file/directory: {source}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    args = [arg for arg in sys.argv[1:] if arg != "--dry-run"]

    if len(args) != 3:
        print("Usage: python add_to_zip.py SOURCE ARCHIVE_ZIP TARGET/PATH/IN/ZIP [--dry-run]")
        sys.exit(2)

    src, archive, target = args[0], args[1], args[2]
    add_to_zip(src, archive, target, dry_run)

    if not dry_run:
        print(f"Added {src} -> {archive} at '{target}'")
