from flask import Flask, render_template, request, redirect, url_for, session, flash
import mysql.connector
from decimal import Decimal
from datetime import date, datetime
import os

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("SECRET_KEY environment variable is not configured.")


def make_json_safe(rows):
    safe_rows = []

    for row in rows:
        safe_row = {}

        for key, value in row.items():
            if isinstance(value, Decimal):
                safe_row[key] = float(value)
            elif isinstance(value, (date, datetime)):
                safe_row[key] = value.strftime("%Y-%m-%d")
            else:
                safe_row[key] = value

        safe_rows.append(safe_row)

    return safe_rows


def get_connection():
    required_variables = [
        "DB_HOST",
        "DB_USER",
        "DB_PASSWORD",
        "DB_NAME"
    ]

    missing_variables = [
        variable
        for variable in required_variables
        if not os.getenv(variable)
    ]

    if missing_variables:
        raise RuntimeError(
            "Missing database environment variables: "
            + ", ".join(missing_variables)
        )

    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        connection_timeout=20
    )

@app.route("/")
def dashboard():
    if "user_email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["RMG", "TA"]:
        session.clear()
        flash("You do not have permission to access the dashboard.", "error")
        return redirect(url_for("login"))

    search = request.args.get("search", "")
    status = request.args.get("status", "All")
    department = request.args.get("department", "All")
    skill = request.args.get("skill", "")

    # keep all your existing dashboard code below

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # KPI: Total employees
    cursor.execute("SELECT COUNT(*) AS total FROM hrms_data")
    headcount = cursor.fetchone()["total"]

    # KPI: Average utilization
    # First calculate employee-wise active allocation, then take average.
    cursor.execute("""
        SELECT ROUND(AVG(TotalAllocation), 2) AS avg_utilization
        FROM (
            SELECT
                h.EmployeeID,
                COALESCE(SUM(
                    CASE
                        WHEN p.ProjectStatus = 'In Progress'
                        THEN p.AllocationPercentage
                        ELSE 0
                    END
                ), 0) AS TotalAllocation
            FROM hrms_data h
            LEFT JOIN project_management p
                ON h.EmployeeID = p.EmployeeID
            GROUP BY h.EmployeeID
        ) emp_alloc
    """)
    avg_utilization = cursor.fetchone()["avg_utilization"] or 0

    # KPI: On bench employees
    # Bench = employee has 0 allocation in active/not-completed projects.
    cursor.execute("""
        SELECT COUNT(*) AS bench_count
        FROM (
            SELECT
                h.EmployeeID,
                COALESCE(SUM(
                    CASE
                        WHEN p.ProjectStatus = 'In Progress'
                        THEN p.AllocationPercentage
                        ELSE 0
                    END
                ), 0) AS TotalAllocation
            FROM hrms_data h
            LEFT JOIN project_management p
                ON h.EmployeeID = p.EmployeeID
            GROUP BY h.EmployeeID
        ) emp_alloc
        WHERE TotalAllocation = 0
    """)
    bench_count = cursor.fetchone()["bench_count"] or 0

    # Departments
    cursor.execute("""
        SELECT DISTINCT Department
        FROM hrms_data
        WHERE Department IS NOT NULL
        ORDER BY Department
    """)
    departments = cursor.fetchall()

    # Category + skills + employee count
    cursor.execute("""
        SELECT
            TRIM(CategoryName) AS CategoryName,
            TRIM(SkillName) AS SkillName,
            COUNT(DISTINCT EmployeeID) AS EmployeeCount
        FROM skills_repository
        WHERE CategoryName IS NOT NULL
          AND TRIM(CategoryName) != ''
          AND SkillName IS NOT NULL
          AND TRIM(SkillName) != ''
        GROUP BY TRIM(CategoryName), TRIM(SkillName)
        ORDER BY TRIM(CategoryName), EmployeeCount DESC
    """)
    category_rows = cursor.fetchall()

    skill_categories = {}

    for row in category_rows:
        category_name = row["CategoryName"]

        if category_name not in skill_categories:
            skill_categories[category_name] = []

        skill_categories[category_name].append({
            "name": row["SkillName"],
            "count": row["EmployeeCount"]
        })

    # Employee cards query
    query = """
        SELECT
            h.EmployeeID,
            h.EmployeeName,
            h.Email,
            h.Department,
            h.Designation,
            h.Location,
            h.BusinessUnit,
            h.EmploymentStatus,
            h.TotalExperience,
            h.ReportingManager,

            COALESCE(alloc.TotalAllocation, 0) AS AllocationPercentage,
            NULL AS ActiveProjects,

            GROUP_CONCAT(DISTINCT
                CASE
                    WHEN p.ProjectStatus IS NOT NULL
                         AND p.ProjectStatus != 'Completed'
                    THEN p.EmployeeRole
                END
                SEPARATOR ', '
            ) AS EmployeeRole,

            GROUP_CONCAT(DISTINCT
                CASE
                    WHEN p.ProjectStatus IS NOT NULL
                         AND p.ProjectStatus != 'Completed'
                    THEN p.ProjectStatus
                END
                SEPARATOR ', '
            ) AS ProjectStatus,

            GROUP_CONCAT(DISTINCT
                CASE
                    WHEN p.ProjectStatus IS NOT NULL
                         AND p.ProjectStatus != 'Completed'
                    THEN p.BillableStatus
                END
                SEPARATOR ', '
            ) AS BillableStatus,

            GROUP_CONCAT(DISTINCT s.SkillName SEPARATOR ', ') AS Skills,
            GROUP_CONCAT(DISTINCT s.SkillLevel SEPARATOR ', ') AS SkillLevels,
            GROUP_CONCAT(DISTINCT TRIM(s.CategoryName) SEPARATOR ', ') AS SkillCategories,

            GROUP_CONCAT(DISTINCT l.CertificationName SEPARATOR ', ') AS Certifications,
            GROUP_CONCAT(DISTINCT l.CourseName SEPARATOR ', ') AS Courses,

            MAX(pm.PerformanceRating) AS PerformanceRating,
            MAX(pm.AwardsRecognition) AS AwardsRecognition,
            MAX(pm.PromotionHistory) AS PromotionHistory,

            GROUP_CONCAT(DISTINCT ph.ProjectName SEPARATOR ', ') AS ProjectHistory

        FROM hrms_data h

        LEFT JOIN (
            SELECT
                EmployeeID,
                SUM(
                    CASE
                        WHEN ProjectStatus = 'In Progress'
                        THEN AllocationPercentage
                        ELSE 0
                    END
                ) AS TotalAllocation
            FROM project_management
            GROUP BY EmployeeID
        ) alloc
            ON h.EmployeeID = alloc.EmployeeID

        LEFT JOIN project_management p
            ON h.EmployeeID = p.EmployeeID

        LEFT JOIN skills_repository s
            ON h.EmployeeID = s.EmployeeID

        LEFT JOIN lms l
            ON h.EmployeeID = l.EmployeeID

        LEFT JOIN performance_management pm
            ON h.EmployeeID = pm.EmployeeID

        LEFT JOIN project_history ph
            ON h.EmployeeID = ph.EmployeeID

        WHERE 1=1
    """

    params = []

    if search:
        query += """
            AND (
                h.EmployeeName LIKE %s
                OR s.SkillName LIKE %s
            )
        """
        like_search = f"%{search}%"
        params.extend([like_search, like_search])

    if department != "All":
        query += " AND h.Department = %s"
        params.append(department)

    if skill:
        query += " AND s.SkillName = %s"
        params.append(skill)

    query += """
        GROUP BY
            h.EmployeeID,
            h.EmployeeName,
            h.Email,
            h.Department,
            h.Designation,
            h.Location,
            h.BusinessUnit,
            h.EmploymentStatus,
            h.TotalExperience,
            h.ReportingManager,
            alloc.TotalAllocation

        ORDER BY
            CASE
                WHEN COUNT(DISTINCT l.CertificationName) > 0
                     AND COUNT(DISTINCT ph.ProjectName) > 0 THEN 1
                WHEN COUNT(DISTINCT ph.ProjectName) > 0 THEN 2
                WHEN COUNT(DISTINCT l.CertificationName) > 0 THEN 3
                ELSE 4
            END,
            h.EmployeeName
    """

    cursor.execute(query, params)
    employees = cursor.fetchall()

    # Load active projects separately so timeline data is never lost through
    # GROUP_CONCAT truncation or duplicate rows from the other joins.
    cursor.execute("""
        SELECT
            EmployeeID,
            ProjectID,
            ProjectName,
            EmployeeRole,
            AllocationPercentage,
            BillableStatus,
            DATE_FORMAT(ProjectStartDate, '%Y-%m-%d') AS ProjectStartDate,
            DATE_FORMAT(ProjectEndDate, '%Y-%m-%d') AS ProjectEndDate,
            CASE
                WHEN ProjectEndDate IS NULL THEN NULL
                ELSE DATEDIFF(ProjectEndDate, CURDATE())
            END AS DaysRemaining,
            CASE
                WHEN ProjectEndDate IS NULL THEN 'Date Not Set'
                WHEN ProjectEndDate < CURDATE() THEN 'Completed'
                WHEN DATEDIFF(ProjectEndDate, CURDATE()) <= 15 THEN 'Closing Soon'
                WHEN DATEDIFF(ProjectEndDate, CURDATE()) <= 30 THEN 'Approaching Completion'
                ELSE 'On Track'
            END AS ProjectHealth
        FROM project_management
        WHERE LOWER(TRIM(ProjectStatus)) = 'in progress'
        ORDER BY
            EmployeeID,
            ProjectEndDate IS NULL,
            ProjectEndDate,
            ProjectName
    """)
    active_project_rows = cursor.fetchall()

    active_projects_by_employee = {}

    for project in active_project_rows:
        employee_key = str(project["EmployeeID"])

        allocation_value = project.get("AllocationPercentage") or 0
        if isinstance(allocation_value, Decimal):
            allocation_value = float(allocation_value)

        days_remaining = project.get("DaysRemaining")
        if days_remaining is not None:
            days_remaining = int(days_remaining)

        active_projects_by_employee.setdefault(employee_key, []).append({
            "ProjectID": project.get("ProjectID") or "",
            "ProjectName": project.get("ProjectName") or "Unnamed Project",
            "EmployeeRole": project.get("EmployeeRole") or "N/A",
            "AllocationPercentage": float(allocation_value),
            "BillableStatus": project.get("BillableStatus") or "N/A",
            "ProjectStartDate": project.get("ProjectStartDate") or "",
            "ProjectEndDate": project.get("ProjectEndDate") or "",
            "DaysRemaining": days_remaining,
            "ProjectHealth": project.get("ProjectHealth") or "Date Not Set"
        })

    filtered_employees = []

    for emp in employees:
        utilization = emp["AllocationPercentage"] or 0

        active_projects = active_projects_by_employee.get(
            str(emp["EmployeeID"]),
            []
        )

        # Show the most urgent active project on the employee card.
        # The full project-wise timeline health is shown in the side panel.
        if active_projects:
            nearest_project = min(
                active_projects,
                key=lambda project: (
                    project.get("DaysRemaining") is None,
                    project.get("DaysRemaining")
                    if project.get("DaysRemaining") is not None
                    else 999999
                )
            )
            emp["TimelineHealth"] = nearest_project["ProjectHealth"]
            emp["NearestProjectName"] = nearest_project["ProjectName"]
            emp["NearestProjectEndDate"] = nearest_project["ProjectEndDate"]
            emp["NearestProjectDaysRemaining"] = nearest_project["DaysRemaining"]
        else:
            emp["TimelineHealth"] = "No Active Project"
            emp["NearestProjectName"] = ""
            emp["NearestProjectEndDate"] = ""
            emp["NearestProjectDaysRemaining"] = None

        emp["ActiveProjectList"] = active_projects

        if utilization == 0:
            emp["Status"] = "On Bench"
        elif utilization < 100:
            emp["Status"] = "Under Utilized"
        elif utilization == 100:
            emp["Status"] = "Utilized"
        else:
            emp["Status"] = "Over Utilized"

        name_parts = emp["EmployeeName"].split()
        emp["Initials"] = "".join([part[0] for part in name_parts[:2]]).upper()

        emp["SkillList"] = emp["Skills"].split(", ") if emp["Skills"] else []
        emp["SkillCategoryList"] = emp["SkillCategories"].split(", ") if emp["SkillCategories"] else []
        emp["CertificationList"] = emp["Certifications"].split(", ") if emp["Certifications"] else []
        emp["CourseList"] = emp["Courses"].split(", ") if emp["Courses"] else []
        emp["ProjectHistoryList"] = emp["ProjectHistory"].split(", ") if emp["ProjectHistory"] else []

        if status == "All" or emp["Status"] == status:
            filtered_employees.append(emp)


    # Drill-down: Total Employees list
    cursor.execute("""
        SELECT
            EmployeeID,
            EmployeeName,
            Department,
            Designation
        FROM hrms_data
        ORDER BY
            Department ASC,
            EmployeeID ASC
    """)
    employee_drill_data = cursor.fetchall()

    # Drill-down: Department-wise utilization
    cursor.execute("""
        SELECT
            h.Department,
            ROUND(AVG(COALESCE(emp_alloc.TotalAllocation, 0)), 2) AS AvgUtilization
        FROM hrms_data h
        LEFT JOIN (
            SELECT
                EmployeeID,
                SUM(
                    CASE
                        WHEN ProjectStatus = 'In Progress'
                        THEN AllocationPercentage
                        ELSE 0
                    END
                ) AS TotalAllocation
            FROM project_management
            GROUP BY EmployeeID
        ) emp_alloc
            ON h.EmployeeID = emp_alloc.EmployeeID
        GROUP BY h.Department
        ORDER BY AvgUtilization DESC
    """)
    dept_util_data = cursor.fetchall()

    # Drill-down: Employee-wise utilization
    cursor.execute("""
        SELECT
            h.EmployeeID,
            h.EmployeeName,
            h.Department,
            h.Designation,
            COALESCE(emp_alloc.TotalAllocation, 0) AS Utilization
        FROM hrms_data h
        LEFT JOIN (
            SELECT
                EmployeeID,
                SUM(
                    CASE
                        WHEN ProjectStatus = 'In Progress'
                        THEN AllocationPercentage
                        ELSE 0
                    END
                ) AS TotalAllocation
            FROM project_management
            GROUP BY EmployeeID
        ) emp_alloc
            ON h.EmployeeID = emp_alloc.EmployeeID
        ORDER BY Utilization DESC, h.EmployeeName
    """)
    utilization_drill_data = cursor.fetchall()

    # Drill-down: Bench employees
    # If your DB has BenchStartDate / ReleaseDate / ProjectEndDate / EndDate, bench days will be calculated.
    # If not, BenchDays will show as Not Available.
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'project_management'
    """)
    pm_columns = [row["COLUMN_NAME"] for row in cursor.fetchall()]

    bench_date_column = None
    for possible_column in ["BenchStartDate", "ReleaseDate", "ProjectEndDate", "EndDate", "ActualEndDate"]:
        if possible_column in pm_columns:
            bench_date_column = possible_column
            break

    if bench_date_column:
        # Use only dates that are today or in the past.
        # Future project end dates must not be used as bench start dates.
        bench_date_sql = f"""
            MAX(
                CASE
                    WHEN p.{bench_date_column} IS NOT NULL
                        AND p.{bench_date_column} <= CURDATE()
                    THEN p.{bench_date_column}
                    ELSE NULL
                END
            )
        """

        bench_days_sql = f"""
            DATEDIFF(
                CURDATE(),
                MAX(
                    CASE
                        WHEN p.{bench_date_column} IS NOT NULL
                            AND p.{bench_date_column} <= CURDATE()
                        THEN p.{bench_date_column}
                        ELSE NULL
                    END
                )
            )
        """
    else:
        bench_days_sql = "NULL"
        bench_date_sql = "NULL"
    cursor.execute(f"""
        SELECT
            h.EmployeeID,
            h.EmployeeName,
            h.Department,
            h.Designation,
            {bench_date_sql} AS BenchStartDate,
            {bench_days_sql} AS BenchDays
        FROM hrms_data h
        LEFT JOIN project_management p
            ON h.EmployeeID = p.EmployeeID
        LEFT JOIN (
            SELECT
                EmployeeID,
                SUM(
                    CASE
                        WHEN ProjectStatus = 'In Progress'
                        THEN AllocationPercentage
                        ELSE 0
                    END
                ) AS TotalAllocation
            FROM project_management
            GROUP BY EmployeeID
        ) emp_alloc
            ON h.EmployeeID = emp_alloc.EmployeeID
        WHERE COALESCE(emp_alloc.TotalAllocation, 0) = 0
        GROUP BY
            h.EmployeeID,
            h.EmployeeName,
            h.Department,
            h.Designation
            ORDER BY
            BenchDays IS NULL,
            BenchDays DESC,
            h.EmployeeName ASC
    """)
    bench_drill_data = cursor.fetchall()

    bench_bucket_data = {
        "0-30 days": 0,
        "31-60 days": 0,
        "60+ days": 0,
        "Not Available": 0
    }

    for bench_emp in bench_drill_data:
        bench_days = bench_emp.get("BenchDays")

        if bench_days is None:
            bench_emp["BenchBucket"] = "Not Available"
        elif 0 <= bench_days <= 30:
            bench_emp["BenchBucket"] = "0-30 days"
        elif 31 <= bench_days <= 60:
            bench_emp["BenchBucket"] = "31-60 days"
        elif bench_days > 60:
            bench_emp["BenchBucket"] = "60+ days"
        else:
            bench_emp["BenchDays"] = None
            bench_emp["BenchBucket"] = "Not Available"
        bench_bucket_data[bench_emp["BenchBucket"]] += 1

    employee_drill_data = make_json_safe(employee_drill_data)
    dept_util_data = make_json_safe(dept_util_data)
    utilization_drill_data = make_json_safe(utilization_drill_data)
    bench_drill_data = make_json_safe(bench_drill_data)

    cursor.close()
    conn.close()

    return render_template(
        "dashboard.html",
        employees=filtered_employees,
        skill_categories=skill_categories,
        departments=departments,
        headcount=headcount,
        avg_utilization=avg_utilization,
        bench_count=bench_count,
        search=search,
        selected_status=status,
        selected_department=department,
        selected_skill=skill,
        employee_drill_data=employee_drill_data,
        dept_util_data=dept_util_data,
        utilization_drill_data=utilization_drill_data,
        bench_drill_data=bench_drill_data,
        bench_bucket_data=bench_bucket_data,
        total_records=len(filtered_employees)
    )
@app.route("/login", methods=["GET", "POST"])
def login():
    # On GET, an already authenticated RMG/TA user can return to the dashboard.
    # On POST, always validate the newly submitted credentials. This prevents an
    # existing session from making a wrong password appear to work.
    if request.method == "GET" and "user_email" in session:
        if session.get("role") in ["RMG", "TA"]:
            return redirect(url_for("dashboard"))
        session.clear()

    email_value = ""

    if request.method == "POST":
        # Clear any previous login before checking the newly entered credentials.
        session.clear()

        email_value = request.form.get("email", "").strip()
        entered_password = request.form.get("password", "")

        if not email_value or not entered_password:
            flash("Please enter both email and password.", "error")
            return render_template("login.html", email_value=email_value)

        conn = None
        cursor = None

        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)

            cursor.execute(
                """
                SELECT Email, password, Role
                FROM `login`
                WHERE LOWER(TRIM(Email)) = LOWER(TRIM(%s))
                LIMIT 1
                """,
                (email_value,)
            )

            user = cursor.fetchone()

            if user is None:
                flash("Email address not found.", "error")
                return render_template("login.html", email_value=email_value)

            stored_password = "" if user.get("password") is None else str(user["password"])

            if entered_password != stored_password:
                flash("Incorrect password.", "error")
                return render_template("login.html", email_value=email_value)

            role = "" if user.get("Role") is None else str(user["Role"]).strip().upper()

            if role not in ["RMG", "TA"]:
                flash("Only RMG and TA users can access this dashboard.", "error")
                return render_template("login.html", email_value=email_value)

            session["user_email"] = user["Email"]
            session["role"] = role
            session.permanent = request.form.get("remember") == "1"

            return redirect(url_for("dashboard"))

        except mysql.connector.Error as error:
            print("Login database error:", error)
            flash("Unable to connect to the database.", "error")
            return render_template("login.html", email_value=email_value)

        finally:
            if cursor:
                cursor.close()

            if conn and conn.is_connected():
                conn.close()

    return render_template("login.html", email_value=email_value)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False
    )