"""Explicit first-install download. The service itself uses the local model cache."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from settings import CONFIG, MODEL_ROOT


def main():
    os.environ.pop('HF_HUB_OFFLINE', None)
    from fastembed import TextEmbedding
    cache = MODEL_ROOT / 'cache' / 'models'
    cache.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(model_name=CONFIG['embedding_model'], cache_dir=str(cache))
    vector = next(iter(model.embed(['shared memory model readiness check'])))
    if len(vector) != 384:
        raise RuntimeError('Configured model must produce 384-dimensional embeddings')
    print('Model ready:', CONFIG['embedding_model'])
    print('Cache:', cache)


if __name__ == '__main__':
    main()
