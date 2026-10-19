# Prompt inventory

- `rag.py`: system instructions for the classic RAG pipeline.
- `agent.py`: Agent system instructions, final-answer and SQL-observation templates, and function-calling tool definitions.
- `text2sql.py`: SQL generation instructions, function-calling definitions, retry, result-summary, and executor-observation templates, plus the few-shot dataset path.
- `data/fewshot_examples.jsonl`: reviewed question-to-SQL examples selected at runtime.

Keep fixed model instructions and templates here. Feature modules remain responsible for preparing dynamic inputs such as retrieved passages, tenant-filtered schemas, query results, and conversation history. Change prompts through normal code review and update relevant evaluation cases when behavior changes.
