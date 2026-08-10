from importlib import import_module
modules = [
    'idx_digest', 'idx_digest.cli', 'idx_digest.pipeline', 'idx_digest.db',
    'idx_digest.downloader', 'idx_digest.extractors', 'idx_digest.idx_client',
    'idx_digest.browser_transport', 'idx_digest.summarizer',
    'idx_digest.observability', 'idx_digest.config', 'idx_digest.timeutils',
]
for name in modules:
    mod = import_module(name)
    print(f'OK {name}: {getattr(mod, "__file__", "built-in")}')
import idx_digest
print(f'VERSION {idx_digest.__version__}')
