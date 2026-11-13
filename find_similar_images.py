#!/usr/bin/env python3
"""
Find and manage similar images in a directory.
Compares images using perceptual hashing and displays similar pairs
side by side, with a delete button under each image.
"""

import os
import sys
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QLabel, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QDialog, QMessageBox
)
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt

try:
    from PIL import Image
    import imagehash

except ImportError:
    print("Required packages not installed. Run:")
    print("  pip install Pillow imagehash PyQt6")
    sys.exit(1)


def get_image_files(directory):
    """Get all image files from a directory (deduplicated by resolved path,
    so a symlink or alias pointing at another image in the folder isn't
    counted as a separate file)."""
    valid_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', ".JPG", ".PNG", ".JPEG"}
    image_files = []
    seen_resolved = set()

    for file in Path(directory).iterdir():
        if file.is_file() and file.suffix.lower() in valid_extensions:
            resolved = file.resolve()
            if resolved in seen_resolved:
                continue
            seen_resolved.add(resolved)
            image_files.append(file)
    
    return sorted(image_files)


def compute_hash(image_path):
    """Compute perceptual hash for an image."""
    try:
        with Image.open(image_path) as img:
            return imagehash.phash(img)
    except Exception as e:
        print(f"Error processing {image_path}:  {e}")
        return None


def find_similar_pairs(image_files, threshold=10):
    """Find pairs of similar images based on hash difference."""
    hashes = {}
    
    print("Computing image hashes...")
    for i, img_path in enumerate(image_files):
        print(f"  Processing {i+1}/{len(image_files)}: {img_path.name}", end='\r')
        img_hash = compute_hash(img_path)
        if img_hash is not None:
            hashes[img_path] = img_hash
    print()
    
    # Find similar pairs
    similar_pairs = []
    paths = list(hashes.keys())
    
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            # Never compare a file against itself (covers the case where
            # the same file appears twice, e.g. via a symlink)
            if paths[i].resolve() == paths[j].resolve():
                continue
            diff = hashes[paths[i]] - hashes[paths[j]]
            if diff <= threshold:
                similar_pairs.append((paths[i], paths[j], diff))
    
    # Sort by similarity (most similar first)
    similar_pairs.sort(key=lambda x: x[2])
    return similar_pairs


def get_file_info(path):
    """Get file size in human-readable format."""
    size = path.stat().st_size
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class ComparisonDialog(QDialog):
    """
    Shows two images side by side, each with its own info label and
    a Delete button underneath. Clicking a delete button deletes that
    file on disk and closes the dialog. There's also a Skip and a
    Quit button.

    self.result_action is set to one of: 'delete1', 'delete2', 'skip', 'quit'
    """

    def __init__(self, img1_path, img2_path, diff, pair_idx, total_pairs):
        super().__init__()
        self.img1_path = img1_path
        self.img2_path = img2_path
        self.result_action = 'skip'  # default if window is closed via 'x'

        similarity = 100 - diff * 2
        self.setWindowTitle(f"Pair {pair_idx}/{total_pairs} — Similarity {similarity}%")

        main_layout = QVBoxLayout(self)

        images_layout = QHBoxLayout()
        images_layout.addLayout(self._make_pane(img1_path, is_first=True))
        images_layout.addLayout(self._make_pane(img2_path, is_first=False))
        main_layout.addLayout(images_layout)

        bottom_layout = QHBoxLayout()
        skip_btn = QPushButton("Skip (keep both)")
        skip_btn.clicked.connect(self._on_skip)
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self._on_quit)
        bottom_layout.addWidget(skip_btn)
        bottom_layout.addWidget(quit_btn)
        main_layout.addLayout(bottom_layout)

    def _make_pane(self, path, is_first):
        """Build a vertical pane: image, filename/size info, delete button."""
        pane = QVBoxLayout()

        pix = QPixmap(str(path))
        pix = pix.scaledToHeight(400, Qt.TransformationMode.SmoothTransformation)
        img_label = QLabel()
        img_label.setPixmap(pix)
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pane.addWidget(img_label)

        info_label = QLabel(f"{path.name}\n{get_file_info(path)}")
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pane.addWidget(info_label)

        delete_btn = QPushButton("Delete this image")
        delete_btn.setStyleSheet("background-color: #c0392b; color: white; padding: 6px;")
        if is_first:
            delete_btn.clicked.connect(self._on_delete_first)
        else:
            delete_btn.clicked.connect(self._on_delete_second)
        pane.addWidget(delete_btn)

        return pane

    def _confirm(self, path):
        reply = QMessageBox.question(
            self, "Confirm delete",
            f"Delete '{path.name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _on_delete_first(self):
        if self._confirm(self.img1_path):
            self.result_action = 'delete1'
            self.accept()

    def _on_delete_second(self):
        if self._confirm(self.img2_path):
            self.result_action = 'delete2'
            self.accept()

    def _on_skip(self):
        self.result_action = 'skip'
        self.accept()

    def _on_quit(self):
        self.result_action = 'quit'
        self.accept()


def show_comparison(img1_path, img2_path, diff, pair_idx, total_pairs):
    """Show the comparison dialog and return the chosen action string."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    dialog = ComparisonDialog(img1_path, img2_path, diff, pair_idx, total_pairs)
    dialog.exec()
    return dialog.result_action


def main():
    # Get directory from command line or prompt
    if len(sys.argv) > 1:
        directory = sys.argv[1]
    else:
        directory = input("Enter the directory path containing images: ").strip()
    
    if not os.path.isdir(directory):
        print(f"Error: '{directory}' is not a valid directory")
        sys.exit(1)
    
    # Get threshold
    threshold = 10  # Default:  images with hash difference <= 10 are considered similar
    
    # Find images
    image_files = get_image_files(directory)
    print(f"Found {len(image_files)} images in '{directory}'")
    
    if len(image_files) < 2:
        print("Need at least 2 images to compare.")
        sys.exit(0)
    
    # Find similar pairs
    similar_pairs = find_similar_pairs(image_files, threshold)
    
    if not similar_pairs:
        print("No similar images found!")
        sys.exit(0)
    
    print(f"\nFound {len(similar_pairs)} similar image pair(s)\n")
    
    # Process each pair
    deleted_files = set()
    
    for idx, (img1, img2, diff) in enumerate(similar_pairs, 1):
        # Skip if either file was already deleted
        if img1 in deleted_files or img2 in deleted_files:
            continue

        if not img1.exists() or not img2.exists():
            continue

        action = show_comparison(img1, img2, diff, idx, len(similar_pairs))

        if action == 'delete1':
            try:
                img1.unlink()
                deleted_files.add(img1)
                print(f"Deleted: {img1.name}")
            except Exception as e:
                print(f"Error deleting file: {e}")
        elif action == 'delete2':
            try:
                img2.unlink()
                deleted_files.add(img2)
                print(f"Deleted: {img2.name}")
            except Exception as e:
                print(f"Error deleting file: {e}")
        elif action == 'skip':
            print("Skipped")
        elif action == 'quit':
            print(f"\nExiting. Deleted {len(deleted_files)} file(s).")
            sys.exit(0)
    
    print(f"\n{'='*60}")
    print(f"Done! Deleted {len(deleted_files)} file(s).")


if __name__ == "__main__":
    main()
