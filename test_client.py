
import asyncio
import json
from fastmcp import Client

MCP_URL = "http://localhost:8000/mcp"

# User-related reference fields to inspect
USER_FIELDS = [
    "caller_id",
    "opened_by",
    "opened_for",
    "assigned_to",
    "resolved_by",
    "closed_by",
    "sys_created_by",
    "sys_updated_by",
    "user"
]


def unwrap(result):
    data = result.data

    # Some FastMCP responses may contain a list with one record
    if isinstance(data, list) and len(data) == 1:
        return data[0]

    return data


def print_record(title, data):
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)

    print(json.dumps(data, indent=4, ensure_ascii=False, default=str))


def check_user_references(title, record):
    print("\n" + "-" * 80)
    print(f"USER REFERENCE CHECK: {title}")
    print("-" * 80)

    found = False

    if not isinstance(record, dict):
        print("No record available for inspection.")
        return

    for field in USER_FIELDS:
        if field not in record:
            continue

        found = True
        reference = record[field]

        if isinstance(reference, dict):
            display_value = reference.get("display_value")
            raw_value = reference.get("value")

            print(f"\nField: {field}")
            print(f"  Display name: {display_value}")
            print(f"  Raw value:    {raw_value}")

            if display_value and raw_value:
                print("  Result:       Display name and ID both available")
            elif display_value:
                print("  Result:       Display name available; raw ID is empty")
            elif raw_value:
                print("  Result:       Raw ID available; display name is empty")
            else:
                print("  Result:       Both values are empty")

        else:
            print(f"\nField: {field}")
            print(f"  Value: {reference}")

    if not found:
        print("No matching user reference fields found in this record.")


async def fetch_record(client, tool, identifier, title):
    print(f"\nFetching {title}...")

    result = await client.call_tool(
        tool,
        {"identifier": identifier}
    )

    record = unwrap(result)

    print_record(title, record)
    check_user_references(title, record)

    return record


async def main():

    async with Client(MCP_URL) as client:

        print("Connected to ServiceNow MCP Server")

        # 1. Incident
        incident = await fetch_record(
            client,
            "get_incidents",
            "INC0010001",
            "INCIDENT"
        )

        # 2. HR Case
        hr_case = await fetch_record(
            client,
            "get_hr_cases",
            "HRC0000618",
            "HR CASE"
        )

        # 3. HR Service
        hr_service = await fetch_record(
            client,
            "get_hr_services",
            "081db1099f231200d9011977677fcf3e",
            "HR SERVICE"
        )

        # 4. HR Profile
        hr_profile = await fetch_record(
            client,
            "get_hr_profiles",
            "HRP0000556",
            "HR PROFILE"
        )

        print("\n" + "=" * 80)
        print("USER REFERENCE CHECK COMPLETED")
        print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
