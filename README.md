# Email Agent

Small learning project built to experiment with **LangChain**, **Flask**, the **Gmail API** and basic **RAG**.

The app can read recent Gmail threads, pass them to a LangChain agent, and generate email drafts/replies.  
For the RAG part, I used a small fake CSV knowledge base about a fictional company called **NexaTech**.

## Tech used

- Python
- Flask
- LangChain / LangGraph
- Gmail API
- FAISS
- OpenAI embeddings

## Notes

This is not a production-ready email automation tool.  
It was built mainly to learn how to combine agents, tool calling and retrieval-augmented generation.

Sensitive files like `.env`, `credentials.json`, `token.json`, email logs and local vector indexes are not included in the repository.
