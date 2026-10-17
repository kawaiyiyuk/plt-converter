"""Load the projection PDF engine without changing the legacy conversion package.

The increment-only image carries the reviewed repository sources in a private
directory. Development loads those same files under the same private module
names; the source files remain maintained once in app/services.
"""
from importlib import import_module
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
from types import ModuleType


def _load_package():
    app_folder = Path(__file__).resolve().parent
    folder = app_folder / '_projection_pdf'
    if not folder.is_dir():
        # Only a full source checkout may use the development source location.
        # Dockerfile.projection is deliberately not copied into the image: an
        # incomplete image must fail, never fall back to its old app.services.
        if not (app_folder.parent / 'Dockerfile.projection').is_file():
            raise RuntimeError('projection private PDF runtime is missing')
        folder = app_folder / 'services'
    required = ('pdf_layout_optimizer.py', 'pdf_to_plt.py', 'pdf_metadata.py')
    if not all((folder / name).is_file() for name in required):
        raise RuntimeError('projection private PDF runtime is incomplete')
    name = __package__ + '._projection_pdf'
    package = ModuleType(name)
    package.__path__ = [str(folder)]
    package.__package__ = name
    package.__spec__ = ModuleSpec(name, loader=None, is_package=True)
    package.__spec__.submodule_search_locations = package.__path__
    sys.modules[name] = package
    return name


_package = _load_package()
engine = import_module(_package + '.pdf_layout_optimizer')
pdf = import_module(_package + '.pdf_to_plt')

# These bindings import only Python definitions. Native PDF loading stays
# deferred until the supervised child has installed its resource limits.
load_fitz = pdf.load_fitz
MAX_PDF_PAGES = pdf.MAX_PDF_PAGES
open_pdf_document = pdf.open_pdf_document
read_pdf_layout_metadata_from_document = pdf.read_pdf_layout_metadata_from_document
validate_pdf_complexity = pdf.validate_pdf_complexity
optimize_pdf_layout = engine.optimize_pdf_layout
