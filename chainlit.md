# Governed Text-to-SQL

Ask a business question in plain language. The application resolves it against an immutable
semantic catalog, applies authorization and mandatory rules, compiles placeholder-only SQL,
validates it, and returns verified rows with provenance.

Try a starter question, or enter your own. If a detail would materially change the answer,
the application pauses and presents only the clarification choices you are authorized to see.

This interface is intended for local development with fixture data. Each submitted question
starts an independent graph thread; chat history is not used as authority or query context.
