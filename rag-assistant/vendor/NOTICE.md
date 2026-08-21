# Bundled Third-Party Packages

This project bundles the following third-party packages in the `vendor/` directory.
Each is used under its respective open source license.

| Package | Version | License | 
|---------|---------|---------|
| [markdownify](https://github.com/matthewwithanm/python-markdownify) | 1.2.3 | MIT |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) | 4.15.0 | MIT |
| [soupsieve](https://github.com/facelessuser/soupsieve) | 2.8.4 | MIT |
| [six](https://github.com/benjaminp/six) | 1.17.0 | MIT |
| [typing_extensions](https://github.com/python/typing_extensions) | 4.16.0 | Python Software Foundation License |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | 5.11.0 | BSD-3-Clause / Apache-2.0 (PDFium) |
| [pypdfium2_raw](https://github.com/pypdfium2-team/pypdfium2) | 5.11.0 | BSD-3-Clause / Apache-2.0 (PDFium 二进制) |
| [pypdfium2_cfg](https://github.com/pypdfium2-team/pypdfium2) | 5.11.0 | BSD-3-Clause / Apache-2.0 |

# Pre-downloaded Model Weights

The following model weight files can be downloaded into `data/models/` via the configuration UI.
Users who redistribute this project must retain the corresponding license files.

## 嵌入模型 (Embedding)

| Model | Source | License |
|-------|--------|---------|
| [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) | HuggingFace | MIT |
| [BAAI/bge-base-zh-v1.5](https://huggingface.co/BAAI/bge-base-zh-v1.5) | HuggingFace | MIT |
| [BAAI/bge-large-zh-v1.5](https://huggingface.co/BAAI/bge-large-zh-v1.5) | HuggingFace | MIT |
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | HuggingFace | MIT |
| [BAAI/bge-large-en-v1.5](https://huggingface.co/BAAI/bge-large-en-v1.5) | HuggingFace | MIT |
| [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) | HuggingFace | MIT |
| [intfloat/multilingual-e5-base](https://huggingface.co/intfloat/multilingual-e5-base) | HuggingFace | MIT |
| [intfloat/multilingual-e5-large-instruc](https://huggingface.co/intfloat/multilingual-e5-large-instruc) | HuggingFace | MIT |
| [sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) | HuggingFace | Apache 2.0 |
| [shibing624/text2vec-base-chinese](https://huggingface.co/shibing624/text2vec-base-chinese) | HuggingFace | Apache 2.0 |
| [maidalun1020/bce-embedding-base_v1](https://huggingface.co/maidalun1020/bce-embedding-base_v1) | HuggingFace | Apache 2.0 |
| [Alibaba-NLP/gte-Qwen2-7B-instruct](https://huggingface.co/Alibaba-NLP/gte-Qwen2-7B-instruct) | HuggingFace | Apache 2.0 |
| [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | HuggingFace | Apache 2.0 |
| [sentence-transformers/all-mpnet-base-v2](https://huggingface.co/sentence-transformers/all-mpnet-base-v2) | HuggingFace | Apache 2.0 |

## 重排序模型 (Reranker)

| Model | Source | License |
|-------|--------|---------|
| [BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) | HuggingFace | MIT |
| [BAAI/bge-reranker-large](https://huggingface.co/BAAI/bge-reranker-large) | HuggingFace | MIT |
| [BAAI/bge-reranker-base](https://huggingface.co/BAAI/bge-reranker-base) | HuggingFace | MIT |
| [BAAI/bge-reranker-v2.5-gemma2-lightweight](https://huggingface.co/BAAI/bge-reranker-v2.5-gemma2-lightweight) | HuggingFace | MIT |
| [mixedbread-ai/mxbai-rerank-base-v1](https://huggingface.co/mixedbread-ai/mxbai-rerank-base-v1) | HuggingFace | Apache 2.0 |
| [Alibaba-NLP/gte-multilingual-reranker-base](https://huggingface.co/Alibaba-NLP/gte-multilingual-reranker-base) | HuggingFace | Apache 2.0 |

## NLI 语义分类模型

| Model | Source | License |
|-------|--------|---------|
| [MoritzLaurer/mDeBERTa-v3-base-mnli-xnli](https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-mnli-xnli) | HuggingFace | MIT |
| [MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli](https://huggingface.co/MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli) | HuggingFace | MIT |
| [cross-encoder/nli-deberta-v3-base](https://huggingface.co/cross-encoder/nli-deberta-v3-base) | HuggingFace | Apache 2.0 |
| [cross-encoder/nli-roberta-base](https://huggingface.co/cross-encoder/nli-roberta-base) | HuggingFace | Apache 2.0 |
| [cross-encoder/nli-distilroberta-base](https://huggingface.co/cross-encoder/nli-distilroberta-base) | HuggingFace | Apache 2.0 |

## LLM 推理模型 (Evidence 语义验证)

| Model | Source | License |
|-------|--------|---------|
| [Qwen/Qwen2.5-0.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct) | HuggingFace | Apache 2.0 |
| [openbmb/MiniCPM5-1B](https://huggingface.co/openbmb/MiniCPM5-1B) | HuggingFace | Apache 2.0 |

All models use permissive open-source licenses compatible with this project's Apache 2.0 license.
