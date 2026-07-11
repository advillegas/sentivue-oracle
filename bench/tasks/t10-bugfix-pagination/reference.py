def paginate(items, page, per_page):
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be >= 1")
    pages = (len(items) + per_page - 1) // per_page
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    return {
        "items": chunk,
        "pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1 and pages > 0,
    }
