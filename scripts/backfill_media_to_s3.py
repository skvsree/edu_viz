from __future__ import annotations

from pathlib import Path

from app.services.storage import get_storage, guess_content_type, media_object_key


def main() -> None:
    root = Path('/opt/edu_viz/app/static/media')
    storage = get_storage()
    storage.ensure_ready()

    uploaded = 0
    skipped = 0

    if not root.exists():
        print('No local media directory found')
        return

    for deck_dir in sorted(root.iterdir()):
        if not deck_dir.is_dir():
            continue
        deck_id = deck_dir.name
        for file_path in sorted(deck_dir.rglob('*')):
            if not file_path.is_file():
                continue
            rel_name = file_path.relative_to(deck_dir).as_posix()
            key = media_object_key(deck_id, rel_name)
            data = file_path.read_bytes()
            storage.save_bytes(key=key, data=data, content_type=guess_content_type(file_path.name))
            uploaded += 1
            print(f'uploaded {key}')

    print(f'done uploaded={uploaded} skipped={skipped}')


if __name__ == '__main__':
    main()
