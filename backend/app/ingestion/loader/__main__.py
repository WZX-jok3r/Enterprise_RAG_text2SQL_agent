"""
`python -m app.ingestion.loader` 的本地冒烟入口。

等价于拆包前 loader.py 末尾的 `if __name__ == "__main__":` 分支：
加载整个知识库并打印统计与前 3 条文档的开头。包化后该分支不会自动执行，
因此单独放在 __main__.py 里保持原有用法可用。
"""

from . import load_all_documents

result = load_all_documents()
print(f"\n加载统计: {result['stats']}")
for doc in result["documents"][:3]:
    print(f"\n--- [{doc.metadata.get('format', 'unknown')}] {doc.metadata.get('type', 'unknown')} ---")
    print(doc.text[:300] + "..." if len(doc.text) > 300 else doc.text)
