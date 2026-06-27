from tvastr.integrations.issue_era_host import IssueEraCodeHost


class _Inner:
    def __init__(self):
        self.calls = []

    def get_file(self, path):
        self.calls.append(("get_file", path))
        return f"main:{path}"

    def get_file_at_ref(self, path, ref):
        self.calls.append(("get_file_at_ref", path, ref))
        return f"{ref}:{path}"

    def list_dir(self, path):
        self.calls.append(("list_dir", path))
        return [f"{path}/main.py"]

    def list_dir_at_ref(self, path, ref):
        self.calls.append(("list_dir_at_ref", path, ref))
        return [f"{path}/{ref}.py"]

    def search_code(self, query, *, limit=5):
        self.calls.append(("search_code", query))
        return ["s.py"]

    def commit_before(self, iso_date):
        return "innersha"

    def buggy_parent_sha(self, pr_number):
        return "bp"

    def open_pull_request(self, draft):
        self.calls.append(("open_pr",))
        return "PR"


def test_reads_served_at_sha():
    inner = _Inner()
    h = IssueEraCodeHost(inner, "SHA1")
    assert h.get_file("a/b.py") == "SHA1:a/b.py"
    assert h.list_dir("a") == ["a/SHA1.py"]


def test_get_file_falls_back_to_main_when_ref_miss():
    class _Miss(_Inner):
        def get_file_at_ref(self, path, ref):
            return None  # file absent at ref
    inner = _Miss()
    h = IssueEraCodeHost(inner, "SHA1")
    assert h.get_file("a/b.py") == "main:a/b.py"  # fell back to inner.get_file


def test_search_and_pr_delegate_unchanged():
    inner = _Inner()
    h = IssueEraCodeHost(inner, "SHA1")
    assert h.search_code("q") == ["s.py"]
    assert h.open_pull_request(object()) == "PR"
    assert ("search_code", "q") in inner.calls
