"""Local API key configuration. Never serialize the key into jobs or responses."""
import getpass
import os
import tempfile
from pathlib import Path


def key_path():
    return Path(os.environ.get('CARBONSHIFT_API_KEY_FILE', str(Path.home()/'.config/carbonshift/electricitymaps.key'))).expanduser()


def read_api_key():
    if 'ELECTRICITYMAPS_API_KEY' in os.environ:
        return os.environ['ELECTRICITYMAPS_API_KEY'].strip()
    path=key_path()
    if not path.exists():
        return ''
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('API key file must be private (chmod 600) and not a symlink.')
    with path.open() as stream:
        key=stream.read(4097).strip()
    if len(key)>4096:
        raise ValueError('API key file is invalid.')
    return key


def configure():
    key=getpass.getpass('Electricity Maps API key (hidden): ').strip()
    if not key or len(key)>4096 or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError('Invalid key; nothing saved.')
    path=key_path();path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='key-')
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(key+'\n');stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)
    print(f'Key saved privately to {path}. Restart CarbonShift. No API request was made.')


if __name__=='__main__':
    try:
        configure()
    except (OSError,ValueError,EOFError,KeyboardInterrupt) as exc:
        print(f'Key not saved: {exc}')
        raise SystemExit(1)
