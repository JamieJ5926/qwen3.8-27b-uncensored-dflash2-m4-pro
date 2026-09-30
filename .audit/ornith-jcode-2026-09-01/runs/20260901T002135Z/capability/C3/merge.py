def merge(defaults, file_values, env):
    result = {}
    for mapping in (defaults, file_values, env):
        for key, value in mapping.items():
            result[key] = value
    return result