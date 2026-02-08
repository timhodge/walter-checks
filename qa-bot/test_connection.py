#!/usr/bin/env python3
"""
test_connection.py — Quick smoke test for the vLLM server.
Run after serve.sh to verify everything works before running a full review.
"""

from openai import OpenAI

VLLM_BASE_URL = "http://localhost:8000/v1"

# Deliberately vulnerable WordPress code — the model should catch both issues
TEST_CODE = '''
function getUserData($id) {
    global $wpdb;
    $result = $wpdb->get_row("SELECT * FROM wp_users WHERE ID = " . $id);
    echo "<h1>Welcome " . $result->display_name . "</h1>";
    return $result;
}
'''

def main():
    print("Connecting to vLLM server...")
    client = OpenAI(base_url=VLLM_BASE_URL, api_key="not-needed")

    models = client.models.list()
    model_name = models.data[0].id
    print(f"Model loaded: {model_name}\n")

    print("Sending deliberately vulnerable code for review...\n")
    print("--- Test Code ---")
    print(TEST_CODE)
    print("-----------------\n")

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": "You are a WordPress security reviewer. "
             "List findings as CRITICAL/WARNING/INFO with file and line context. "
             "Do NOT write code fixes — describe what should change."},
            {"role": "user", "content": f"Review this code for security issues:\n{TEST_CODE}"},
        ],
        temperature=0.1,
        max_tokens=1024,
    )

    print("--- Review Result ---")
    print(response.choices[0].message.content)
    print("---------------------")
    print(f"\nTokens: {response.usage.prompt_tokens} in / {response.usage.completion_tokens} out")

    # Basic validation — the model should catch SQL injection
    content = response.choices[0].message.content.lower()
    if "sql" in content or "injection" in content or "prepare" in content:
        print("\n✓ Model correctly identified SQL injection — QA Bot is ready!")
    else:
        print("\n⚠ Model may not have caught SQL injection — check output above")

    print("\nNext: python qa-bot/review.py repo repos/<your-repo> --profile wordpress")


if __name__ == "__main__":
    main()
