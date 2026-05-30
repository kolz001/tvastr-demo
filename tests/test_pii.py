from tvastr.pii import contains_pii, redact


def test_redacts_email_and_api_key():
    text = "user jane.doe@example.com token sk-ant-REDACTEDABC123"
    redacted, found = redact(text)
    assert "jane.doe@example.com" not in redacted
    assert "sk-ant-REDACTEDABC123" not in redacted
    assert "EMAIL" in found
    assert "API_KEY" in found


def test_redacts_credentialed_url_and_ip():
    text = "connect to https://admin@search.internal:9200 from 10.0.1.42"
    redacted, found = redact(text)
    assert "admin@search.internal" not in redacted
    assert "CREDENTIAL_URL" in found
    assert "IP" in found


def test_clean_text_is_untouched():
    text = "PipelineConnectError: type mismatch between components"
    redacted, found = redact(text)
    assert redacted == text
    assert found == []
    assert contains_pii(text) is False
