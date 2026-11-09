"""Submit a mix of sample jobs to a running API, including ones that retry and fail.

    python scripts/create_test_jobs.py --base-url http://localhost:8000 --email you@example.com --password ...

Without --email a throwaway account is created.
"""
import argparse
import uuid

import httpx

SAMPLES = [
    {"job_type": "generate_report", "payload": {"report_id": "report-123", "format": "csv"}, "priority": 5},
    {"job_type": "process_file", "priority": 7, "payload": {
        "file_name": "notes.txt", "content": "TaskFlow processes jobs.\nWorkers run handlers.",
        "operations": ["line_count", "word_count", "top_words"]}},
    {"job_type": "send_notification", "priority": 9, "payload": {
        "notification_id": f"welcome-{uuid.uuid4().hex[:6]}", "channel": "email",
        "recipient": "someone@example.com", "message": "Welcome to TaskFlow"}},
    {"job_type": "simulated_work", "payload": {"duration_ms": 2000}, "priority": 1},
    {"job_type": "simulated_work", "payload": {"duration_ms": 100, "fail_until_attempt": 2}, "max_attempts": 3},
    {"job_type": "simulated_work", "payload": {"fail_until_attempt": 5}, "max_attempts": 2},
    {"job_type": "simulated_work", "payload": {"fail_until_attempt": 1, "failure": "permanent"}},
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--email")
    parser.add_argument("--password")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        if args.email:
            login = client.post("/auth/login", json={"email": args.email, "password": args.password})
            login.raise_for_status()
            token = login.json()["access_token"]
        else:
            signup = client.post("/auth/signup", json={
                "email": f"demo-{uuid.uuid4().hex[:8]}@example.com", "password": "demo-password-123",
                "full_name": "Demo User", "phone_number": "0000000",
            })
            signup.raise_for_status()
            token = signup.json()["ascess_token"]
            print("Created demo account:", signup.json()["email"], "/ demo-password-123")

        headers = {"Authorization": f"Bearer {token}"}
        for sample in SAMPLES:
            response = client.post("/api/v1/jobs", json=sample, headers=headers)
            response.raise_for_status()
            print(f"{sample['job_type']:<18} -> {response.json()['job_id']}")


if __name__ == "__main__":
    main()
