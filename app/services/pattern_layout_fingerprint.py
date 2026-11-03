"""Separate reproducible identities for recognition and export dependencies."""
import ast
import hashlib
import json
from pathlib import Path
import platform
import sys

ROOT=Path(__file__).parent


def sha(data):return hashlib.sha256(data).hexdigest()


def function_components(path,names):
    text=Path(path).read_text();tree=ast.parse(text)
    return {node.name:sha(ast.get_source_segment(text,node).encode()) for node in tree.body if isinstance(node,(ast.FunctionDef,ast.ClassDef)) and node.name in names}


def environment():
    import shapely,numpy,zlib
    return {'shapely':shapely.__version__,'geos':shapely.geos_version_string,'geos_capi':shapely.geos_capi_version_string,
            'numpy':numpy.__version__,'zlib_runtime':zlib.ZLIB_RUNTIME_VERSION,'zlib_compile':zlib.ZLIB_VERSION,'python':sys.version,'implementation':sys.implementation.name,
            'architecture':platform.machine(),'system':platform.system(),'release':platform.release(),'libc':platform.libc_ver()}


def recognition_components():
    files=sorted((ROOT/'pattern_layout').glob('*.py'))+[ROOT/name for name in ('plt_parser.py','number_tokens.py','pattern_layout_resources.py','pattern_layout_variants.py')]
    parts={str(path.relative_to(ROOT)):sha(path.read_bytes()) for path in files}
    parts.update({'api.'+name:value for name,value in function_components(ROOT/'pattern_layout_api.py',{'source_paths','recognize_source','source_phase_prefixes','runtime_version'}).items()})
    text=(ROOT/'pattern_layout_api.py').read_text()
    parts['api.imports']=sha('\n'.join(ast.get_source_segment(text,node) for node in ast.parse(text).body if isinstance(node,(ast.Import,ast.ImportFrom))).encode())
    return parts


def recognition_runtime_version():
    implementation=function_components(__file__,{'sha','function_components','environment','recognition_components','recognition_runtime_version'})
    return sha(json.dumps({'schema':'recognition-v2','components':recognition_components(),'implementation':implementation,'environment':environment()},sort_keys=True,separators=(',',':')).encode())


def export_runtime_version():
    parts=function_components(ROOT/'pattern_layout_api.py',{'export_project','source_paths','source_phase_prefixes','finite','hpgl_decimal','PatternLayoutExportError'})
    for name in ('plt_parser.py','number_tokens.py','pattern_layout/parse_source.py','pattern_layout_verify.py'):
        parts[name]=sha((ROOT/name).read_bytes())
    return sha(json.dumps({'schema':'export-v1','components':parts,'environment':environment()},sort_keys=True,separators=(',',':')).encode())
