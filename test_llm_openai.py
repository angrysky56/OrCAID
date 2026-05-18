import litellm
try:
    response = litellm.completion(
        model="openai/MiniMax-M2.7",
        api_key="sk-test",
        base_url="https://api.minimax.chat/v1",
        messages=[{"role": "user", "content": "hello"}]
    )
    print(response)
except Exception as e:
    print("Error:", e)
