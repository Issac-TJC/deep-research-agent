# 长文档检索与本地多模态解析

本改造在现有不可变 `SourceVersion → EvidenceSpan → Claim → Report` 链路前增加“发现层”。索引只负责告诉 Researcher 应该读哪里；最终 quote 仍由应用从冻结的 `ParsedDocument` 字符范围复制。

## 运行组件

- PostgreSQL 17 + pgvector 0.8.6：保存租户隔离的 chunk、全文索引、768 维向量和索引任务。
- Indexer：用数据库租约异步解析上传、稳定分块、先提交词法索引，再批量写入 embedding。失败最多重试三次；向量失败保留 `lexical_ready`。
- Embedding service：固定 `Alibaba-NLP/gte-multilingual-base@9bbca17`，CPU 默认四线程，检测到 CUDA 时自动使用 GPU；运行网络没有数据库、对象存储或供应商凭据。
- Parser：固定 Docling 2.126.0，使用 RapidOCR（`zh-Hans,en`）、accurate TableFormer、公式、图片分类、图表抽取和 `SmolVLM-256M-Instruct@7e3e67e`。模型只在镜像构建时下载，运行时离线。

## 状态和接口

- `POST /uploads`：普通文本同步返回 201；增强 PDF 最多等待 30 秒，未完成返回 202。
- `GET /uploads/{upload_id}`：返回 `pending / processing / ready / failed`、进度和稳定 source 映射。
- `GET /sources/{source_id}`：返回不可变 source/document 及实时 index 状态。
- `search_sources(query, limit)`：来源范围由服务端从任务授权 source IDs 注入；返回的每项均为 `evidence=false`。
- `GET /evidence-spans/{span_id}/crop`：读取 OCR、公式或视觉证据的租户私有截图。

Run profile 支持 `retrieval_strategy=sequential|lexical|hybrid`、`max_retrieval_calls` 和 `parser_mode=native|auto|enhanced`。设置 `RETRIEVAL_SHADOW=true` 会计算并持久化初始混合检索结果，但不把 hit 交给 Researcher，便于上线对照。

## 运维

首次升级必须让所有活跃 run 结束，然后执行：

```sh
docker compose up --build -d postgres minio embedding parser
docker compose run --rm migrate research migrate
docker compose up -d indexer api worker web
docker compose run --rm migrate research reindex --all
```

重建使用新的 `index_version`，最后一个可用索引在新版本达到 `lexical_ready` 前继续服务。当前查询在至多 24 个授权来源内做精确 cosine，不创建 HNSW；只有基准的 p95 超过 1 秒后才应引入按租户分区的近似索引。

冻结检索集使用 JSON 数组，每题包含 `id/query/source_ids/relevant[{source_id,start,end}]`，错误码或版本号题设置 `exact_identifier=true`。运行 `research retrieval-benchmark DATASET --enforce` 会比较 lexical、dense 和 hybrid，并执行 150 题、Recall@8、精确标识符、hybrid 不退化及 p95 门禁。

## 可信边界

- 原文、解析结果和索引版本都不原地覆盖。
- OCR／表格文字可成为 source statement，但置信度低于 0.80 时附加 `visual_confirmation_required`。
- 公式识别始终携带 `formula_recognition` 标记；视觉模型描述保存为 `DerivedArtifact`，Claim attribution 强制为 `inference`。
- Docling 和原生解析都失败时，上传记录保留原始对象 key 并标记 `failed`，不会产生无来源结论。
