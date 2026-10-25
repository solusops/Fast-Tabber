import json

import httpx
import pytest

from fast_tabber.preflight import EMBEDDING, GEMMA, red_image
from fast_tabber.vision import describe_image


@pytest.mark.asyncio
async def test_vision_requires_exact_model_and_valid_output(tmp_path):
    (tmp_path / "models.json").write_text(json.dumps({"models": {GEMMA: "verified"}}))
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": GEMMA, "digest": "verified"},
                                                        {"name": EMBEDDING, "digest": "embedding"}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"thinking": {"values": [False, True]}})
        assert json.loads(request.content)["messages"][0]["images"] == [red_image()]
        return httpx.Response(200, json={"message": {"content": '{"description":"A solid red image."}'}})

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        return httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handler))

    assert await describe_image(red_image(), tmp_path, factory) == "A solid red image."
    assert len(requests) == 3
    (tmp_path / "models.json").write_text(json.dumps({"models": {GEMMA: "different"}}))
    with pytest.raises(RuntimeError, match="digest"):
        await describe_image(red_image(), tmp_path, factory)
    assert len(requests) == 4, "Unverified model must never receive screenshots"


@pytest.mark.asyncio
@pytest.mark.parametrize("image", ["bad", "", "aGVsbG8="])
async def test_vision_rejects_invalid_images_before_inference(tmp_path, image):
    with pytest.raises(ValueError):
        await describe_image(image, tmp_path)
