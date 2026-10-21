# Third-party notices

Shared Mem0 is a local integration layer around independently maintained libraries. This source repository does not include a Python interpreter, a virtual environment, third-party package source distributions, or model weights. Dependencies are installed separately with `pip`; the full pinned package inventory is in `requirements.lock.txt`.

The repository's own license applies to the original integration code. It does not replace the licenses of its dependencies or of any models downloaded during use. When preparing a future bundled executable or offline distribution, retain the license and notice files shipped with every included dependency and model.

## Main runtime dependencies

The following versions and license identifiers were checked against the installed distribution metadata and, where present, the distribution's license files on 2026-10-21. This table is an attribution guide for the main components, rather than an exhaustive license inventory of every transitive dependency.

| Component | Version | License | Upstream |
| --- | --- | --- | --- |
| mem0ai | 2.0.20 | Apache-2.0 | [PyPI package](https://pypi.org/project/mem0ai/) |
| FastEmbed | 0.8.0 | Apache-2.0, as stated in its shipped LICENSE | [qdrant/fastembed](https://github.com/qdrant/fastembed) |
| qdrant-client | 1.19.1 | Apache-2.0 | [qdrant/qdrant-client](https://github.com/qdrant/qdrant-client) |
| MCP Python SDK | 2.2.0 | MIT | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) |
| FastAPI | 0.141.1 | MIT | [fastapi/fastapi](https://github.com/fastapi/fastapi) |
| Uvicorn | 0.53.0 | BSD-3-Clause | [uvicorn.dev](https://uvicorn.dev/) |
| ONNX Runtime | 1.30.0 | MIT | [onnxruntime.ai](https://onnxruntime.ai/) |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | [pyca/cryptography](https://github.com/pyca/cryptography) |
| OpenAI Python SDK | 3.14.1 | Apache-2.0 | [openai/openai-python](https://github.com/openai/openai-python) |
| Hugging Face Tokenizers | 0.23.2 | Apache-2.0, as stated in its distribution classifier | [huggingface/tokenizers](https://github.com/huggingface/tokenizers) |
| Hugging Face Hub | 1.31.0 | Apache-2.0 | [huggingface/huggingface_hub](https://github.com/huggingface/huggingface_hub) |
| pywin32 | 312 | PSF, as stated in its distribution metadata; individual bundled components have their own notices | [PyPI package](https://pypi.org/project/pywin32/) |

FastEmbed's installed package also carries a NOTICE with model-specific terms for several optional Jina AI and Google models. Those optional models are not selected by this project's default configuration. FastEmbed's library license must not be interpreted as the license of every model it can download. Changing the embedding model requires checking that model's own terms.

## Default embedding model

The configured model identifier is `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. FastEmbed uses the corresponding quantized ONNX model from `Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`.

Both the [original model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) and the [Qdrant ONNX model card](https://huggingface.co/Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q) state Apache-2.0. The ONNX card identifies the model as a quantized port of the original. These model cards were checked on 2026-10-21. Model weights and tokenizer assets are downloaded separately and are not committed to this repository.

## Optional client integration

`app/pi-memory.ts` imports `@earendil-works/pi-coding-agent` and `typebox` from the host Pi environment. Their implementations are not included in this repository or in the Python lock file. Install Pi and its dependencies through their own distribution process, and retain the notices provided with them if redistributing those packages.

Codex, WorkBuddy, Pi, Claude Code, Google Drive, and other referenced product names identify compatible clients or services. Their clients and connected services are not included in this repository. This project does not claim endorsement by those projects or vendors.
