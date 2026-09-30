def slug(text):
    import re
    result = text.lower()
    result = re.sub(r'[ _\-]+', '-', result)
    result = result.strip('-')
    return result