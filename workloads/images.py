"""Allowlisted image transformation. No user code, network or shell execution."""
import argparse
import hashlib
import io
import json
import os
import time
import warnings
import zipfile
from pathlib import Path
from PIL import Image, ImageOps

MAX_RESULT=64*1024*1024
Image.MAX_IMAGE_PIXELS=8_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)


def run(input_dir, output_dir, max_edge, quality, expected_hash, timeout):
    start=time.monotonic()
    raw=(input_dir/'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_hash:
        raise ValueError('Input manifest hash mismatch')
    manifest=json.loads(raw); files=manifest['files']
    if not 1<=len(files)<=20 or not 64<=max_edge<=4096 or not 40<=quality<=95:
        raise ValueError('Invalid batch limits')
    output_dir.mkdir(parents=True,exist_ok=True)
    partial=output_dir/'results.partial'
    results=[]; output_bytes=0
    print(json.dumps({'event':'started','total':len(files),'processed':0}),flush=True)
    try:
        with zipfile.ZipFile(partial,'w',zipfile.ZIP_STORED) as archive:
            for index,item in enumerate(files):
                if time.monotonic()-start>=timeout:
                    raise TimeoutError('Image processing exceeded the configured timeout')
                if item['file']!=f'{index+1:03d}.image':
                    raise ValueError('Invalid input path')
                data=(input_dir/item['file']).read_bytes()
                if len(data)>5*1024*1024 or hashlib.sha256(data).hexdigest()!=item['sha256']:
                    raise ValueError('Input file hash mismatch')
                with Image.open(io.BytesIO(data)) as source:
                    if source.format not in ('JPEG','PNG') or source.width*source.height>8_000_000 or getattr(source,'n_frames',1)!=1:
                        raise ValueError('Unsupported image')
                    source.load()
                    image=ImageOps.exif_transpose(source)
                    image.thumbnail((max_edge,max_edge),Image.Resampling.LANCZOS)
                    rgba=image.convert('RGBA')
                    rgb=Image.new('RGB',rgba.size,'white');rgb.paste(rgba,mask=rgba.getchannel('A'))
                    buffer=io.BytesIO();rgb.save(buffer,format='JPEG',quality=quality,optimize=True)
                    encoded=buffer.getvalue(); output_bytes+=len(encoded)
                    if output_bytes>MAX_RESULT-65536:
                        raise ValueError('Output exceeds 64 MiB')
                    name=f'{index+1:03d}.jpg';archive.writestr(name,encoded)
                    results.append({'original_name':item['original_name'],'output_name':name,'input_bytes':len(data),'output_bytes':len(encoded),'width':rgb.width,'height':rgb.height})
                print(json.dumps({'event':'progress','processed':index+1,'total':len(files)}),flush=True)
            report={'batch_id':manifest['batch_id'],'manifest_sha256':expected_hash,'max_edge':max_edge,'quality':quality,'processed':len(results),'files':results,'input_bytes':sum(r['input_bytes'] for r in results),'output_bytes':output_bytes,'processing_seconds':time.monotonic()-start,'note':'JPEG outputs; aspect ratio preserved, no upscaling, transparency flattened on white, EXIF metadata removed. Output may be larger than input. No measured energy or carbon savings.'}
            archive.writestr('report.json',json.dumps(report,indent=2))
        with partial.open('rb') as stream: os.fsync(stream.fileno())
        partial.replace(output_dir/'results.zip')
        print(json.dumps({'event':'completed','processed':len(results),'total':len(files),'output_bytes':output_bytes}),flush=True)
    finally:
        partial.unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,default=Path('/input'))
    parser.add_argument('--output',type=Path,default=Path('/output'))
    parser.add_argument('--max-edge',type=int,required=True)
    parser.add_argument('--quality',type=int,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--timeout',type=int,required=True)
    args=parser.parse_args()
    run(args.input,args.output,args.max_edge,args.quality,args.manifest_sha256,args.timeout)
