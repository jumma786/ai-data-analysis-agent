from rag.pipeline import chunk_text, build_store


def test_chunking():
    text = " ".join(str(i) for i in range(2000))
    chunks = chunk_text(text, size=500, overlap=50)
    assert len(chunks) > 1


def test_vector_store_retrieval():
    store = build_store("the refund policy allows returns within 30 days. "
                        "shipping is free over fifty pounds.")
    hits = store.query("what is the refund policy", k=1)
    assert "refund" in hits[0].lower()
