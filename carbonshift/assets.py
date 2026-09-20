"""Bounded immutable input bundles and per-job results on the local execution host."""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import uuid
import warnings
import zipfile
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from pydantic import Field
from .models import Model

MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
MAX_BODY = 29 * 1024 * 1024
MAX_PIXELS = 8_000_000
MAX_RESULT = 64 * 1024 * 1024

class UploadFile(Model):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=7 * 1024 * 1024)

class UploadBatch(Model):
    files: list[UploadFile] = Field(min_length=1, max_length=20)


def root():
    path=Path(os.environ.get('CARBONSHIFT_ASSETS', str(Path.home()/'.local/share/carbonshift/assets'))).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def identified(folder, identifier):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('Invalid artifact identifier.')
    path = root()/folder/identifier
    if path.is_symlink() or path.resolve() != path:
        raise ValueError('Artifact path must not contain symlinks.')
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_upload(payload):
    prepared, total = [], 0
    for index, item in enumerate(payload.files):
        try:
            raw = base64.b64decode(item.content, validate=True)
            total += len(raw)
            if len(raw) > MAX_FILE or total > MAX_TOTAL:
                raise ValueError('Limit: 5 MiB per image, 20 MiB per batch.')
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as im:
                    if im.format not in ('PNG', 'JPEG') or getattr(im, 'n_frames', 1) != 1:
                        raise ValueError('Only single-frame JPEG and PNG images are supported.')
                    if im.width * im.height > MAX_PIXELS:
                        raise ValueError('Each image must be at most 8 megapixels.')
                    width, height = im.size
                    im.verify()
            name = re.sub(r'[\x00-\x1f\x7f]', '', item.name.replace('\\', '/').split('/')[-1]) or 'image'
            prepared.append((raw, {'file':f'{index+1:03d}.image','original_name':name,'sha256':digest(raw),'bytes':len(raw),'width':width,'height':height}))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError('Invalid or oversized image; no batch was saved.') from exc
    batch_id = uuid.uuid4().hex
    destination = identified('inputs', batch_id)
    destination.mkdir(parents=True, mode=0o755)
    try:
        manifest = {'batch_id':batch_id, 'files':[record for _,record in prepared], 'total_bytes':total}
        content = json.dumps(manifest, sort_keys=True, separators=(',',':')).encode()
        for raw, record in prepared:
            p=destination/record['file'];p.write_bytes(raw);p.chmod(0o444)
        p=destination/'manifest.json';p.write_bytes(content);p.chmod(0o444)
        destination.chmod(0o555)
        return {**manifest, 'manifest_sha256':digest(content)}
    except BaseException:
        destination.chmod(0o755);shutil.rmtree(destination)
        raise


def validate_bundle(spec):
    directory = identified('inputs', spec['batch_id'])
    p = directory/'manifest.json'
    if p.is_symlink() or p.stat().st_size > 32768:
        raise ValueError('Invalid input manifest.')
    raw = p.read_bytes()
    if digest(raw) != spec['manifest_sha256']:
        raise ValueError('Input manifest changed after preview. Upload a fresh batch.')
    manifest = json.loads(raw)
    records = manifest['files']
    if not 1 <= len(records) <= 20 or manifest['batch_id'] != spec['batch_id']:
        raise ValueError('Invalid input manifest.')
    total = 0
    for index, item in enumerate(records):
        if item['file'] != f'{index+1:03d}.image':
            raise ValueError('Invalid input filename.')
        p=directory/item['file']
        if p.is_symlink() or p.stat().st_size > MAX_FILE:
            raise ValueError('Invalid input file.')
        raw=p.read_bytes();total += len(raw)
        if digest(raw) != item['sha256'] or len(raw) != item['bytes'] or total > MAX_TOTAL:
            raise ValueError('Input files changed after upload. Upload a fresh batch.')
    return directory, manifest


def output_directory(job_id):
    path = identified('outputs', job_id)
    path.mkdir(parents=True, exist_ok=True)
    # Only this job's directory is writable inside the unprivileged container.
    # The enclosing assets directory is private to the application user.
    path.chmod(0o777)
    return path


def result_path(job_id):
    p = identified('outputs', job_id)/'results.zip'
    if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_RESULT:
        raise ValueError('Result archive is absent or invalid.')
    return p


def inspect_result(job):
    spec=job['snapshot']['execution']['image_batch']
    _, inputs=validate_bundle(spec)
    p=result_path(job['id'])
    with zipfile.ZipFile(p) as archive:
        expected=[f'{i+1:03d}.jpg' for i in range(len(inputs['files']))]+['report.json']
        if sorted(archive.namelist()) != sorted(expected) or sum(i.file_size for i in archive.infolist()) > MAX_RESULT:
            raise ValueError('Result archive has unexpected files or size.')
        if archive.getinfo('report.json').file_size > 65536 or archive.testzip() is not None:
            raise ValueError('Result archive is damaged.')
        report=json.loads(archive.read('report.json'))
        if report['batch_id'] != spec['batch_id'] or report['manifest_sha256'] != spec['manifest_sha256'] or report['processed'] != len(inputs['files']):
            raise ValueError('Result does not match the queued input batch.')
        if report['max_edge'] != spec['max_edge'] or report['quality'] != spec['quality']:
            raise ValueError('Result does not match the queued image settings.')
    return {'filename':'results.zip','bytes':p.stat().st_size,'sha256':digest(p.read_bytes()),'report':report}


def progress_from_logs(logs):
    last = None
    for line in logs.splitlines():
        try:
            record=json.loads(line)
            if record.get('event') in ('progress','completed') and type(record.get('processed')) is int and type(record.get('total')) is int and 0 <= record['processed'] <= record['total'] <= 20:
                last={'processed':record['processed'],'total':record['total']}
        except (ValueError, AttributeError):
            pass
    return last
