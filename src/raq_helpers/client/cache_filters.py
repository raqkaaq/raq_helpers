from hishel import BaseFilter, Request, Response
import re

class URLPatternFilter(BaseFilter[Request]):
    def __init__(self, pattern: str):
        self.pattern = re.compile(pattern)
    def needs_body(self) -> bool:
        return False
    def apply(self, item: Request, body: bytes | None) -> bool:
        return bool(self.pattern.search(str(item.url)))
    def __repr__(self) -> str:
        return f"URLPatternFilter(pattern={self.pattern.pattern})"
    def __str__(self) -> str:
        return f"URLPatternFilter with pattern: {self.pattern.pattern}"

class MethodFilter(BaseFilter[Request]):
    def __init__(self, methods: list[str]):
        self.methods = [method.upper() for method in methods]
    def needs_body(self) -> bool:
        return False
    def apply(self, item: Request, body: bytes | None) -> bool:
        return item.method.upper() in self.methods
    def __repr__(self) -> str:
        return f"MethodFilter(methods={self.methods})"
    def __str__(self) -> str:
        return f"MethodFilter with methods: {self.methods}"

class StatusCodeFilter(BaseFilter[Response]):
    def __init__(self, status_codes: list[int]):
        self.status_codes = status_codes
    def needs_body(self) -> bool:
        return False
    def apply(self, item: Response, body: bytes | None) -> bool:
        return item.status_code in self.status_codes
    def __repr__(self) -> str:
        return f"StatusCodeFilter(status_codes={self.status_codes})"
    def __str__(self) -> str:
        return f"StatusCodeFilter with status codes: {self.status_codes}"

class ContentTypeFilter(BaseFilter[Response]):
    def __init__(self, content_types: list[str]):
        self.content_types = content_types
    def needs_body(self) -> bool:
        return False
    def apply(self, item: Response, body: bytes | None) -> bool:
        content_type = item.headers.get("content-type", "")
        return any(ct in content_type for ct in self.content_types)
    def __repr__(self) -> str:
        return f"ContentTypeFilter(content_types={self.content_types})"
    def __str__(self) -> str:
        return f"ContentTypeFilter with content types: {self.content_types}"

class HeaderFilter(BaseFilter[Response]):
    def __init__(self, header_name: str, header_value: str):
        self.header_name = header_name
        self.header_value = header_value
    def needs_body(self) -> bool:
        return False
    def apply(self, item: Response, body: bytes | None) -> bool:
        return item.headers.get(self.header_name) == self.header_value
    def __repr__(self) -> str:
        return f"HeaderFilter(header_name={self.header_name}, header_value={self.header_value})"
    def __str__(self) -> str:
        return f"HeaderFilter with header: {self.header_name} and value: {self.header_value}"
