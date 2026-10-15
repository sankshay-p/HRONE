from datetime import datetime, date, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import app, db

client = TestClient(app)


def test_awkward_case_overnight_shift():
    # Employee with overnight shift: 22:00 to 06:00
    db.employees.delete_many({"emp_code": "EMP5001"})
    db.attendance_logs.delete_many({"emp_code": "EMP5001"})

    res_emp = client.post(
        "/employees",
        json={
            "emp_code": "EMP5001",
            "name": "Night Owl",
            "email": "night@example.com",
            "department": "Security",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-07-01",
        },
    )
    assert res_emp.status_code == 201, res_emp.text

    # 1. Punch in at 05:40 IST on July 7 (00:10 UTC on July 7)
    # Since 05:40 < 06:00, R1 says attendance date must be 2026-07-06!
    dt_early_utc = datetime(2026, 7, 7, 0, 10, 0, tzinfo=timezone.utc)
    ms_early = int(dt_early_utc.timestamp() * 1000)

    res_in = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP5001", "punched_at": ms_early, "status": "PRESENT"},
    )
    assert res_in.status_code == 201, res_in.text
    assert res_in.json()["date"] == "2026-07-06"

    # Punch out at 06:40 IST on July 7 (01:10 UTC on July 7) -> 40 min overtime past 06:00 shift end
    dt_out_utc = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out_utc.timestamp() * 1000)

    res_out = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP5001", "punched_at": ms_out},
    )
    assert res_out.status_code == 200, res_out.text
    out_data = res_out.json()
    assert out_data["overtime_minutes"] == 40
    # From 00:10 to 01:10 UTC = 1.0 hour
    assert out_data["work_hours"] == 1.0
    assert out_data["half_day"] is True  # < 4.50 hrs


def test_awkward_case_mid_month_joiner():
    # Joined on 2026-07-20 (Monday)
    # Working days in July 2026 from July 20 to July 31:
    # July 20-24: 5 days
    # July 27-31: 5 days
    # Total = 10 working days
    db.employees.delete_many({"emp_code": "EMP5002"})
    db.attendance_logs.delete_many({"emp_code": "EMP5002"})

    res_emp = client.post(
        "/employees",
        json={
            "emp_code": "EMP5002",
            "name": "Mid Month Joiner",
            "email": "joiner@example.com",
            "department": "Finance",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-20",
        },
    )
    assert res_emp.status_code == 201

    res_m = client.get("/analytics/employees/EMP5002/monthly?month=2026-07")
    assert res_m.status_code == 200
    data = res_m.json()
    assert data["working_days"] == 10
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.0


def test_awkward_case_weekend_records():
    # Saturday punch record
    # 2026-07-11 is a Saturday
    db.employees.delete_many({"emp_code": "EMP5003"})
    db.attendance_logs.delete_many({"emp_code": "EMP5003"})

    res_emp = client.post(
        "/employees",
        json={
            "emp_code": "EMP5003",
            "name": "Weekend Warrior",
            "email": "weekend@example.com",
            "department": "Finance",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        },
    )
    assert res_emp.status_code == 201

    # 10:15:00 IST on Saturday July 11 (04:45 UTC) -> 45 min late
    dt_in = datetime(2026, 7, 11, 4, 45, 0, tzinfo=timezone.utc)
    ms_in = int(dt_in.timestamp() * 1000)
    res_in = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP5003", "punched_at": ms_in},
    )
    assert res_in.status_code == 201
    assert res_in.json()["late_minutes"] == 45

    # Punch out at 19:30:00 IST (14:00 UTC) -> 60 min overtime
    dt_out = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)
    res_out = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP5003", "punched_at": ms_out},
    )
    assert res_out.status_code == 200
    assert res_out.json()["overtime_minutes"] == 60

    # Check monthly analytics:
    # R7: Weekend records still contribute to late and overtime totals,
    # BUT present_days only counts records that fall on Mon-Fri!
    res_m = client.get("/analytics/employees/EMP5003/monthly?month=2026-07")
    assert res_m.status_code == 200
    m_data = res_m.json()
    assert m_data["present_days"] == 0.0  # Saturday does not count towards present_days
    assert m_data["total_late_minutes"] == 45
    assert m_data["late_count"] == 1
    assert m_data["total_overtime_minutes"] == 60


def test_awkward_case_leaderboard_ties_and_limit():
    # Create 4 employees with late ties:
    # EMP6001: 50 min
    # EMP6002: 30 min
    # EMP6003: 30 min
    # EMP6004: 10 min
    # Expected ranking:
    # Rank 1: EMP6001 (50)
    # Rank 2: EMP6002 (30)
    # Rank 2: EMP6003 (30) (ordered by emp_code asc)
    # Rank 4: EMP6004 (10) (rank 3 is skipped!)
    # If limit=2: Ranks <= 2 should be returned -> EMP6001, EMP6002, EMP6003 (3 rows)!
    dept = "TieDept"
    db.employees.delete_many({"department": dept})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP600"}})

    for code, late_mins in [("EMP6001", 50), ("EMP6002", 30), ("EMP6003", 30), ("EMP6004", 10)]:
        res_emp = client.post(
            "/employees",
            json={
                "emp_code": code,
                "name": f"User {code}",
                "email": f"{code.lower()}@example.com",
                "department": dept,
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-07-01",
            },
        )
        assert res_emp.status_code == 201
        # Direct seed log
        db.attendance_logs.insert_one({
            "emp_code": code,
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 30, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
            "work_hours": 8.5,
            "late_minutes": late_mins,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })

    # Test leaderboard with limit=2
    res = client.get(f"/analytics/leaderboard/late?month=2026-07&limit=2&department={dept}")
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    # Limit applies after ranking (rank <= 2), so all 3 tied at rank <= 2 must be returned!
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP6001"
    assert items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP6002"
    assert items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP6003"
    assert items[2]["rank"] == 2

    # Test leaderboard with limit=4: EMP6004 should be rank 4 (rank 3 skipped!)
    res4 = client.get(f"/analytics/leaderboard/late?month=2026-07&limit=4&department={dept}")
    assert res4.status_code == 200
    items4 = res4.json()["items"]
    assert len(items4) == 4
    assert items4[3]["emp_code"] == "EMP6004"
    assert items4[3]["rank"] == 4


def test_awkward_case_headcount_with_no_logs():
    # Department with 2 employees: 1 has logs, 1 has ZERO logs
    dept = "NoLogDept"
    db.employees.delete_many({"department": dept})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP7001", "EMP7002"]}})

    r1 = client.post(
        "/employees",
        json={
            "emp_code": "EMP7001",
            "name": "Has Logs",
            "email": "nl1@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        },
    )
    assert r1.status_code == 201

    r2 = client.post(
        "/employees",
        json={
            "emp_code": "EMP7002",
            "name": "Zero Logs",
            "email": "nl2@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        },
    )
    assert r2.status_code == 201

    # Log only for EMP7001
    db.attendance_logs.insert_one({
        "emp_code": "EMP7001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # Department summary
    res = client.get(f"/analytics/departments/summary?month=2026-07&department={dept}")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["headcount"] == 2  # Must count both EMP7001 and EMP7002!
    assert items[0]["present_days"] == 1.0


def test_awkward_case_trend_gap_filling_missing_weekday():
    # 2026-07-06 (Mon) has a record
    # 2026-07-07 (Tue) has NO record
    # 2026-07-08 (Wed) has a record
    dept = "GapDept"
    db.employees.delete_many({"department": dept})
    db.attendance_logs.delete_many({"emp_code": "EMP8001"})

    r = client.post(
        "/employees",
        json={
            "emp_code": "EMP8001",
            "name": "Gap Worker",
            "email": "gap@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        },
    )
    assert r.status_code == 201

    for d in ["2026-07-06", "2026-07-08"]:
        db.attendance_logs.insert_one({
            "emp_code": "EMP8001",
            "date": d,
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })

    res = client.get(f"/analytics/departments/{dept}/trend?from=2026-07-06&to=2026-07-08")
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["date"] == "2026-07-06"
    assert items[0]["present_count"] == 1.0
    assert items[0]["attendance_rate"] == 1.0

    assert items[1]["date"] == "2026-07-07"
    assert items[1]["is_working_day"] is True
    assert items[1]["headcount"] == 1
    assert items[1]["present_count"] == 0.0
    assert items[1]["attendance_rate"] == 0.0  # Missing weekday has rate 0.0

    assert items[2]["date"] == "2026-07-08"
    assert items[2]["present_count"] == 1.0
    assert items[2]["attendance_rate"] == 1.0
