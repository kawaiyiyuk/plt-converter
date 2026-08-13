def iter_number_tokens(text):
    start = None
    for index, character in enumerate(text):
        if character in ', \t\r\n':
            if start is not None:
                yield text[start:index]
                start = None
        elif start is None:
            start = index
    if start is not None:
        yield text[start:]
