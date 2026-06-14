from app import crypto


def test_encrypt_decrypt_roundtrip():
    secret = "s3cr3t-p@ss"
    enc = crypto.encrypt(secret)
    assert enc != secret  # not plaintext
    assert secret not in enc
    assert crypto.decrypt(enc) == secret


def test_decrypt_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        crypto.decrypt("not-a-valid-token")
