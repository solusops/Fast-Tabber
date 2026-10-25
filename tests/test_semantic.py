import pytest

from fast_tabber.semantic import cluster_vectors, cosine, validate_embeddings


def test_embedding_response_is_batched_finite_and_nonempty():
    assert validate_embeddings({"embeddings": [[1.0, 0.0], [0.0, 1.0]]}, 2, 2) == [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(RuntimeError, match="invalid"):
        validate_embeddings({"embeddings": [[1.0], [float("nan")]]}, 2, 2)


def test_cosine_and_local_clusters_use_vector_similarity():
    vectors = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0], [0.01, 0.99]]
    assert cosine(vectors[0], vectors[1]) > 0.99
    assert cosine(vectors[0], vectors[2]) == 0
    assert cluster_vectors(vectors, distance=0.2) == [0, 0, 1, 1]
