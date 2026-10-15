from fastapi.testclient import TestClient
from app.main import app, db

client = TestClient(app)


def test_weekly_timing_endpoint():
    res = client.get("/analytics/employees/EMP0001/weekly?start_date=2026-07-06")
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["emp_code"] == "EMP0001"
    assert data["department"] == "Engineering"
    assert data["start_date"] == "2026-07-06"
    assert data["end_date"] == "2026-07-12"

    # Summary
    summary = data["summary"]
    assert "total_work_hours" in summary
    assert "total_time_formatted" in summary
    assert "avg_daily_work_hours" in summary
    assert "days_present" in summary
    assert "scheduled_shift_hours" in summary
    assert "scheduled_weekly_hours" in summary
    assert summary["days_present"] >= 1
    assert "h " in summary["total_time_formatted"] or "m" in summary["total_time_formatted"]

    # 7 Days
    days = data["days"]
    assert len(days) == 7
    days_names = [d["day_name"] for d in days]
    assert days_names == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    # Monday check (2026-07-06 has log in sample data)
    mon = days[0]
    assert mon["date"] == "2026-07-06"
    assert mon["status"] == "PRESENT"
    assert mon["work_hours"] == 9.12
    assert mon["amount_of_time"] == "9h 07m"
    assert mon["punch_in_time"] == "09:28:00"
    assert mon["punch_out_time"] == "18:35:00"


def test_peer_comparison_endpoint():
    res = client.get("/analytics/employees/EMP0001/comparison?month=2026-07")
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["department"] == "Engineering"

    # Employee metrics
    emp_m = data["employee_metrics"]
    assert emp_m["total_work_hours"] > 0
    assert "total_time_formatted" in emp_m
    assert emp_m["rank_in_department"] >= 1

    # Department benchmark
    dept_b = data["department_benchmark"]
    assert dept_b["department"] == "Engineering"
    assert dept_b["headcount"] >= 1
    assert "avg_work_hours" in dept_b
    assert "peers" in dept_b
    assert len(dept_b["peers"]) == dept_b["headcount"]

    # Current employee should be tagged in peers
    curr_tags = [p for p in dept_b["peers"] if p["is_current"]]
    assert len(curr_tags) == 1
    assert curr_tags[0]["emp_code"] == "EMP0001"

    # Company benchmark
    comp_b = data["company_benchmark"]
    assert comp_b["total_employees"] >= dept_b["headcount"]
    assert "percentile_rank" in comp_b
    assert 0 <= comp_b["percentile_rank"] <= 100
    dist = comp_b["distribution"]
    assert dist["min"] <= dist["q25"] <= dist["median"] <= dist["q75"] <= dist["max"]


def test_dashboard_route():
    for route in ("/dashboard", "/"):
        res = client.get(route)
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "Clario WorkPulse" in res.text
        assert "weekly-bar-svg" in res.text
        assert "compare-bar-svg" in res.text


def test_weekly_and_comparison_edge_cases():
    # 404 for unknown employee
    res_404_w = client.get("/analytics/employees/EMP9999/weekly")
    assert res_404_w.status_code == 404

    res_404_c = client.get("/analytics/employees/EMP9999/comparison")
    assert res_404_c.status_code == 404

    # 422 for invalid date format
    res_422_w = client.get("/analytics/employees/EMP0001/weekly?start_date=invalid-date")
    assert res_422_w.status_code == 422

    res_422_c = client.get("/analytics/employees/EMP0001/comparison?month=2026-7")
    assert res_422_c.status_code == 422

    # Zero-log employee (EMP7002 has zero logs in NoLogDept)
    res_zero = client.get("/analytics/employees/EMP7002/weekly?start_date=2026-07-06")
    assert res_zero.status_code == 200
    zero_data = res_zero.json()
    assert zero_data["summary"]["total_work_hours"] == 0.0
    assert zero_data["summary"]["days_present"] == 0
    assert zero_data["summary"]["total_time_formatted"] == "—"
