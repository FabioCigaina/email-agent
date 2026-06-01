When given a JSON, it represents a thread:
        {
            "thread_id" : thread_id,
            "emails" : list of emails
        }
        summarize the thread.
        Then, only if necessary you should call the tool 'create_reply'
        to create a response to continue the conversation.
        Don't ask for clarification if you understand that a response is needed,
        just write a generic response which can later be filled with real details.