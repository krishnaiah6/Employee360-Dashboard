from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort
import mysql.connector
from decimal import Decimal
from datetime import date, datetime
import os
import uuid
from urllib.parse import urlparse, unquote
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Always set a strong SECRET_KEY in Render. The fallback is only for local development.
app.secret_key = os.getenv("SECRET_KEY", "local-development-secret-key")

# Render terminates HTTPS before forwarding requests to Flask. These settings keep
# session cookies secure in production while still allowing localhost development.
is_production = os.getenv("RENDER", "").lower() == "true" or os.getenv("FLASK_ENV") == "production"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=is_production,
)

CERTIFICATE_UPLOAD_FOLDER = os.getenv(
    "CERTIFICATE_UPLOAD_FOLDER",
    os.path.join(app.root_path, "uploads", "certificates"),
)
ALLOWED_CERTIFICATE_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
MAX_CERTIFICATE_FILE_SIZE = 5 * 1024 * 1024

app.config["MAX_CONTENT_LENGTH"] = MAX_CERTIFICATE_FILE_SIZE
os.makedirs(CERTIFICATE_UPLOAD_FOLDER, exist_ok=True)

def allowed_certificate_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_CERTIFICATE_EXTENSIONS
    )

def is_valid_http_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False

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
    """Connect to Railway on Render and local MySQL during development.

    On Render, use the DB_HOST, DB_PORT, DB_USER, DB_PASSWORD and DB_NAME
    environment variables. DATABASE_URL/MYSQL_URL are also supported.
    """
    is_render = os.getenv("RENDER", "").lower() == "true"

    # Prefer the explicit DB_* variables configured in Render.
    explicit_host = os.getenv("DB_HOST")
    if explicit_host:
        config = {
            "host": explicit_host,
            "port": int(os.getenv("DB_PORT", "3306")),
            "user": os.getenv("DB_USER", "root"),
            "password": os.getenv("DB_PASSWORD", ""),
            "database": os.getenv("DB_NAME", "railway"),
        }
    else:
        database_url = os.getenv("DATABASE_URL") or os.getenv("MYSQL_PUBLIC_URL") or os.getenv("MYSQL_URL")

        if database_url:
            parsed = urlparse(database_url)
            if parsed.scheme not in {"mysql", "mysql2"}:
                raise ValueError("Database URL must use the mysql:// scheme")

            config = {
                "host": parsed.hostname,
                "port": parsed.port or 3306,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "database": unquote(parsed.path.lstrip("/")) or "railway",
            }
        elif not is_render:
            config = {
                "host": os.getenv("LOCAL_DB_HOST", "localhost"),
                "port": int(os.getenv("LOCAL_DB_PORT", "3306")),
                "user": os.getenv("LOCAL_DB_USER", "root"),
                "password": os.getenv("LOCAL_DB_PASSWORD", ""),
                "database": os.getenv("LOCAL_DB_NAME", "employee360_test"),
            }
        else:
            raise RuntimeError(
                "Render database variables are missing. Add DB_HOST, DB_PORT, "
                "DB_USER, DB_PASSWORD and DB_NAME."
            )

    missing = [key for key in ("host", "user", "password", "database") if not config.get(key)]
    if missing:
        raise RuntimeError("Missing database configuration: " + ", ".join(missing))

    return mysql.connector.connect(
        **config,
        connection_timeout=20,
        autocommit=False,
        charset="utf8mb4",
        use_unicode=True,
    )


@app.route("/")
def dashboard():
    if "user_email" not in session:
        return redirect(url_for("login"))

    if session.get("role") not in ["RMG", "TA"]:
        session.clear()
        flash("You do not have permission to access the dashboard.", "error")
        return redirect(url_for("login"))

    search = request.args.get("search", "").strip()
    status = request.args.get("status", "All").strip()
    department = request.args.get("department", "All").strip()
    skill = request.args.get("skill", "").strip()

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # A project is treated as current only while it is in progress and its
        # end date has not passed. This protects the dashboard even if the
        # scheduled archive event has not run yet for the day.
        active_project_condition = """
            LOWER(TRIM(ProjectStatus)) = 'in progress'
            AND (ProjectEndDate IS NULL OR ProjectEndDate >= CURDATE())
        """

        # -------------------------------------------------------------
        # KPI SUMMARY
        # -------------------------------------------------------------
        cursor.execute("SELECT COUNT(*) AS total FROM hrms_data")
        headcount = cursor.fetchone()["total"] or 0

        cursor.execute(f"""
            SELECT ROUND(AVG(TotalAllocation), 2) AS avg_utilization
            FROM (
                SELECT
                    h.EmployeeID,
                    COALESCE(a.TotalAllocation, 0) AS TotalAllocation
                FROM hrms_data h
                LEFT JOIN (
                    SELECT
                        EmployeeID,
                        SUM(COALESCE(AllocationPercentage, 0)) AS TotalAllocation
                    FROM project_management
                    WHERE {active_project_condition}
                    GROUP BY EmployeeID
                ) a ON h.EmployeeID = a.EmployeeID
            ) employee_utilization
        """)
        avg_utilization = cursor.fetchone()["avg_utilization"] or 0

        cursor.execute(f"""
            SELECT COUNT(*) AS bench_count
            FROM hrms_data h
            LEFT JOIN (
                SELECT
                    EmployeeID,
                    SUM(COALESCE(AllocationPercentage, 0)) AS TotalAllocation
                FROM project_management
                WHERE {active_project_condition}
                GROUP BY EmployeeID
            ) a ON h.EmployeeID = a.EmployeeID
            WHERE COALESCE(a.TotalAllocation, 0) = 0
        """)
        bench_count = cursor.fetchone()["bench_count"] or 0

        # -------------------------------------------------------------
        # FILTER DATA
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT DISTINCT Department
            FROM hrms_data
            WHERE Department IS NOT NULL
              AND TRIM(Department) != ''
            ORDER BY Department
        """)
        departments = cursor.fetchall()

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
            ORDER BY TRIM(CategoryName), EmployeeCount DESC, TRIM(SkillName)
        """)
        category_rows = cursor.fetchall()

        normalized_categories = {}
        for row in category_rows:
            category_name = (row.get("CategoryName") or "").strip()
            skill_name = (row.get("SkillName") or "").strip()
            employee_count = int(row.get("EmployeeCount") or 0)

            if not category_name or not skill_name:
                continue

            category_key = category_name.casefold()
            skill_key = skill_name.casefold()

            category_data = normalized_categories.setdefault(
                category_key,
                {"display_name": category_name, "skills": {}}
            )

            skill_data = category_data["skills"].setdefault(
                skill_key,
                {"name": skill_name, "count": 0}
            )
            skill_data["count"] += employee_count

        skill_categories = {}
        for category_data in sorted(
            normalized_categories.values(),
            key=lambda item: item["display_name"].casefold()
        ):
            skill_categories[category_data["display_name"]] = sorted(
                category_data["skills"].values(),
                key=lambda item: (-item["count"], item["name"].casefold())
            )

        # -------------------------------------------------------------
        # EMPLOYEE CARDS
        # Each supporting table is aggregated before joining so multiple
        # projects, skills, certifications, and history rows do not multiply
        # one another and distort the results.
        # -------------------------------------------------------------
        query = f"""
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

                COALESCE(a.TotalAllocation, 0) AS AllocationPercentage,
                pm_meta.EmployeeRole,
                pm_meta.ProjectStatus,
                pm_meta.BillableStatus,

                skills.Skills,
                skills.SkillLevels,
                skills.SkillCategories,

                learning.Certifications,
                learning.Courses,

                performance.PerformanceRating,
                performance.AwardsRecognition,
                performance.PromotionHistory,

                history.ProjectHistory

            FROM hrms_data h

            LEFT JOIN (
                SELECT
                    EmployeeID,
                    SUM(COALESCE(AllocationPercentage, 0)) AS TotalAllocation
                FROM project_management
                WHERE {active_project_condition}
                GROUP BY EmployeeID
            ) a ON h.EmployeeID = a.EmployeeID

            LEFT JOIN (
                SELECT
                    EmployeeID,
                    GROUP_CONCAT(DISTINCT EmployeeRole ORDER BY EmployeeRole SEPARATOR ', ') AS EmployeeRole,
                    GROUP_CONCAT(DISTINCT ProjectStatus ORDER BY ProjectStatus SEPARATOR ', ') AS ProjectStatus,
                    GROUP_CONCAT(DISTINCT BillableStatus ORDER BY BillableStatus SEPARATOR ', ') AS BillableStatus
                FROM project_management
                WHERE {active_project_condition}
                GROUP BY EmployeeID
            ) pm_meta ON h.EmployeeID = pm_meta.EmployeeID

            LEFT JOIN (
                SELECT
                    EmployeeID,
                    GROUP_CONCAT(DISTINCT TRIM(SkillName) ORDER BY TRIM(SkillName) SEPARATOR ', ') AS Skills,
                    GROUP_CONCAT(DISTINCT TRIM(SkillLevel) ORDER BY TRIM(SkillLevel) SEPARATOR ', ') AS SkillLevels,
                    GROUP_CONCAT(DISTINCT TRIM(CategoryName) ORDER BY TRIM(CategoryName) SEPARATOR ', ') AS SkillCategories
                FROM skills_repository
                GROUP BY EmployeeID
            ) skills ON h.EmployeeID = skills.EmployeeID

            LEFT JOIN (
                SELECT
                    EmployeeID,
                    GROUP_CONCAT(DISTINCT TRIM(CertificationName) ORDER BY TRIM(CertificationName) SEPARATOR ', ') AS Certifications,
                    GROUP_CONCAT(DISTINCT TRIM(CourseName) ORDER BY TRIM(CourseName) SEPARATOR ', ') AS Courses
                FROM lms
                GROUP BY EmployeeID
            ) learning ON h.EmployeeID = learning.EmployeeID

            LEFT JOIN (
                SELECT
                    EmployeeID,
                    MAX(PerformanceRating) AS PerformanceRating,
                    GROUP_CONCAT(DISTINCT AwardsRecognition ORDER BY AwardsRecognition SEPARATOR ', ') AS AwardsRecognition,
                    GROUP_CONCAT(DISTINCT PromotionHistory ORDER BY PromotionHistory SEPARATOR ', ') AS PromotionHistory
                FROM performance_management
                GROUP BY EmployeeID
            ) performance ON h.EmployeeID = performance.EmployeeID

            LEFT JOIN (
                SELECT
                    EmployeeID,
                    GROUP_CONCAT(
                        DISTINCT ProjectName
                        ORDER BY EndDate DESC, ProjectName
                        SEPARATOR ', '
                    ) AS ProjectHistory
                FROM project_history
                GROUP BY EmployeeID
            ) history ON h.EmployeeID = history.EmployeeID

            WHERE 1 = 1
        """

        params = []

        if search:
            query += """
                AND (
                    LOWER(TRIM(h.EmployeeName)) LIKE LOWER(TRIM(%s))
                    OR EXISTS (
                        SELECT 1
                        FROM skills_repository search_skill
                        WHERE search_skill.EmployeeID = h.EmployeeID
                          AND LOWER(TRIM(search_skill.SkillName)) LIKE LOWER(TRIM(%s))
                    )
                )
            """
            like_search = f"%{search}%"
            params.extend([like_search, like_search])

        if department != "All":
            query += " AND h.Department = %s"
            params.append(department)

        if skill:
            query += """
                AND EXISTS (
                    SELECT 1
                    FROM skills_repository selected_skill
                    WHERE selected_skill.EmployeeID = h.EmployeeID
                      AND LOWER(TRIM(selected_skill.SkillName)) = LOWER(TRIM(%s))
                )
            """
            params.append(skill)

        query += """
            ORDER BY
                CASE
                    WHEN learning.Certifications IS NOT NULL
                         AND history.ProjectHistory IS NOT NULL THEN 1
                    WHEN history.ProjectHistory IS NOT NULL THEN 2
                    WHEN learning.Certifications IS NOT NULL THEN 3
                    ELSE 4
                END,
                h.EmployeeName
        """

        cursor.execute(query, params)
        employees = cursor.fetchall()

        # -------------------------------------------------------------
        # CURRENT PROJECT DETAILS FOR CARD + SIDE PANEL
        # -------------------------------------------------------------
        cursor.execute(f"""
            SELECT
                EmployeeID,
                ProjectID,
                ProjectName,
                EmployeeRole,
                COALESCE(AllocationPercentage, 0) AS AllocationPercentage,
                BillableStatus,
                DATE_FORMAT(ProjectStartDate, '%Y-%m-%d') AS ProjectStartDate,
                DATE_FORMAT(ProjectEndDate, '%Y-%m-%d') AS ProjectEndDate,
                CASE
                    WHEN ProjectEndDate IS NULL THEN NULL
                    ELSE DATEDIFF(ProjectEndDate, CURDATE())
                END AS DaysRemaining,
                CASE
                    WHEN ProjectEndDate IS NULL THEN 'Date Not Set'
                    WHEN DATEDIFF(ProjectEndDate, CURDATE()) <= 15 THEN 'Closing Soon'
                    WHEN DATEDIFF(ProjectEndDate, CURDATE()) <= 30 THEN 'Approaching Completion'
                    ELSE 'On Track'
                END AS ProjectHealth
            FROM project_management
            WHERE {active_project_condition}
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
            utilization = emp.get("AllocationPercentage") or 0
            if isinstance(utilization, Decimal):
                utilization = float(utilization)
            utilization = float(utilization)
            emp["AllocationPercentage"] = round(utilization, 2)

            active_projects = active_projects_by_employee.get(str(emp["EmployeeID"]), [])
            emp["ActiveProjectList"] = active_projects

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

            if utilization <= 0:
                emp["Status"] = "On Bench"
            elif utilization < 100:
                emp["Status"] = "Under Utilized"
            elif utilization == 100:
                emp["Status"] = "Utilized"
            else:
                emp["Status"] = "Over Utilized"

            name_parts = (emp.get("EmployeeName") or "").split()
            emp["Initials"] = "".join(
                part[0] for part in name_parts[:2] if part
            ).upper() or "E"

            emp["SkillList"] = emp["Skills"].split(", ") if emp.get("Skills") else []
            emp["SkillCategoryList"] = (
                emp["SkillCategories"].split(", ")
                if emp.get("SkillCategories") else []
            )
            emp["CertificationList"] = (
                emp["Certifications"].split(", ")
                if emp.get("Certifications") else []
            )
            emp["CourseList"] = emp["Courses"].split(", ") if emp.get("Courses") else []
            emp["ProjectHistoryList"] = (
                emp["ProjectHistory"].split(", ")
                if emp.get("ProjectHistory") else []
            )

            if status == "All" or emp["Status"] == status:
                filtered_employees.append(emp)

        # -------------------------------------------------------------
        # KPI DRILL-DOWN DATA
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT EmployeeID, EmployeeName, Department, Designation
            FROM hrms_data
            ORDER BY Department ASC, EmployeeID ASC
        """)
        employee_drill_data = cursor.fetchall()

        cursor.execute(f"""
            SELECT
                h.Department,
                ROUND(AVG(COALESCE(a.TotalAllocation, 0)), 2) AS AvgUtilization
            FROM hrms_data h
            LEFT JOIN (
                SELECT
                    EmployeeID,
                    SUM(COALESCE(AllocationPercentage, 0)) AS TotalAllocation
                FROM project_management
                WHERE {active_project_condition}
                GROUP BY EmployeeID
            ) a ON h.EmployeeID = a.EmployeeID
            GROUP BY h.Department
            ORDER BY AvgUtilization DESC, h.Department
        """)
        dept_util_data = cursor.fetchall()

        cursor.execute(f"""
            SELECT
                h.EmployeeID,
                h.EmployeeName,
                h.Department,
                h.Designation,
                COALESCE(a.TotalAllocation, 0) AS Utilization
            FROM hrms_data h
            LEFT JOIN (
                SELECT
                    EmployeeID,
                    SUM(COALESCE(AllocationPercentage, 0)) AS TotalAllocation
                FROM project_management
                WHERE {active_project_condition}
                GROUP BY EmployeeID
            ) a ON h.EmployeeID = a.EmployeeID
            ORDER BY Utilization DESC, h.EmployeeName
        """)
        utilization_drill_data = cursor.fetchall()

        # Bench status comes from zero current allocation. Bench duration is
        # derived only when the employee has no current project rows and a past
        # completed-project end date is available. Employees with a zero-percent
        # current assignment are correctly On Bench, but their duration remains
        # unavailable because an active assignment end date is not a bench date.
        cursor.execute(f"""
            SELECT
                h.EmployeeID,
                h.EmployeeName,
                h.Department,
                h.Designation,
                CASE
                    WHEN COALESCE(a.ActiveProjectCount, 0) = 0
                         AND history.LastCompletedEndDate IS NOT NULL
                         AND history.LastCompletedEndDate <= CURDATE()
                    THEN history.LastCompletedEndDate
                    ELSE NULL
                END AS BenchStartDate,
                CASE
                    WHEN COALESCE(a.ActiveProjectCount, 0) = 0
                         AND history.LastCompletedEndDate IS NOT NULL
                         AND history.LastCompletedEndDate <= CURDATE()
                    THEN DATEDIFF(CURDATE(), history.LastCompletedEndDate)
                    ELSE NULL
                END AS BenchDays
            FROM hrms_data h
            LEFT JOIN (
                SELECT
                    EmployeeID,
                    COUNT(*) AS ActiveProjectCount,
                    SUM(COALESCE(AllocationPercentage, 0)) AS TotalAllocation
                FROM project_management
                WHERE {active_project_condition}
                GROUP BY EmployeeID
            ) a ON h.EmployeeID = a.EmployeeID
            LEFT JOIN (
                SELECT EmployeeID, MAX(EndDate) AS LastCompletedEndDate
                FROM project_history
                WHERE EndDate IS NOT NULL
                  AND EndDate <= CURDATE()
                GROUP BY EmployeeID
            ) history ON h.EmployeeID = history.EmployeeID
            WHERE COALESCE(a.TotalAllocation, 0) = 0
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

            if bench_days is None or int(bench_days) < 0:
                bench_emp["BenchDays"] = None
                bench_emp["BenchBucket"] = "Not Available"
            elif 0 <= int(bench_days) <= 30:
                bench_emp["BenchBucket"] = "0-30 days"
            elif 31 <= int(bench_days) <= 60:
                bench_emp["BenchBucket"] = "31-60 days"
            else:
                bench_emp["BenchBucket"] = "60+ days"

            bench_bucket_data[bench_emp["BenchBucket"]] += 1

        employee_drill_data = make_json_safe(employee_drill_data)
        dept_util_data = make_json_safe(dept_util_data)
        utilization_drill_data = make_json_safe(utilization_drill_data)
        bench_drill_data = make_json_safe(bench_drill_data)

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

    except mysql.connector.Error as error:
        print("RMG dashboard database error:", error)
        flash("Unable to load the dashboard from the database.", "error")
        return redirect(url_for("login"))

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    # On GET, an already authenticated RMG/TA user can return to the dashboard.
    # On POST, always validate the newly submitted credentials. This prevents an
    # existing session from making a wrong password appear to work.
    if request.method == "GET" and "user_email" in session:
        if session.get("role") in ["RMG", "TA"]:
            return redirect(url_for("dashboard"))
        if session.get("role") == "EMPLOYEE":
            return redirect(url_for("employee_dashboard"))
        if session.get("role") in ["L&D", "LND"]:
            return redirect(url_for("employee_dashboard"))
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

            if role not in ["RMG", "TA", "EMPLOYEE", "L&D", "LND"]:
                flash("Your role does not currently have an assigned page.", "error")
                return render_template("login.html", email_value=email_value)

            session["user_email"] = user["Email"]
            session["role"] = role
            session.permanent = request.form.get("remember") == "1"

            if role == "EMPLOYEE":
                return redirect(url_for("employee_dashboard"))

            if role in ["L&D", "LND"]:
                return redirect(url_for("employee_dashboard"))

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



@app.route("/employee-dashboard")
def employee_dashboard():
    if "user_email" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    if session.get("role") not in ["EMPLOYEE", "L&D", "LND"]:
        flash("You do not have permission to access the employee page.", "error")
        return redirect(url_for("dashboard"))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                EmployeeID,
                EmployeeName,
                Email,
                Department,
                Designation,
                Location,
                BusinessUnit,
                EmploymentStatus,
                TotalExperience,
                ReportingManager
            FROM hrms_data
            WHERE LOWER(TRIM(Email)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (session["user_email"],))

        employee = cursor.fetchone()

        if employee is None:
            if session.get("role") in ["L&D", "LND"]:
                flash(
                    "No employee profile is linked to this L&D login email. "
                    "The L&D dashboard is still available.",
                    "error"
                )
                return redirect(url_for("lnd_dashboard"))

            session.clear()
            flash(
                "No employee record is linked to this login email. "
                "Please verify that Login.Email and hrms_data.Email are the same.",
                "error"
            )
            return redirect(url_for("login"))

        employee_id = employee["EmployeeID"]

        cursor.execute("""
            SELECT
                ProjectID,
                ProjectName,
                EmployeeRole,
                AllocationPercentage,
                ProjectStatus,
                BillableStatus,
                DATE_FORMAT(ProjectStartDate, '%Y-%m-%d') AS ProjectStartDate,
                DATE_FORMAT(ProjectEndDate, '%Y-%m-%d') AS ProjectEndDate
            FROM project_management
            WHERE EmployeeID = %s
              AND LOWER(TRIM(ProjectStatus)) = 'in progress'
            ORDER BY ProjectEndDate IS NULL, ProjectEndDate, ProjectName
        """, (employee_id,))
        active_projects = cursor.fetchall()

        total_utilization = 0
        for project in active_projects:
            allocation = project.get("AllocationPercentage") or 0
            if isinstance(allocation, Decimal):
                allocation = float(allocation)
            project["AllocationPercentage"] = float(allocation)
            total_utilization += float(allocation)

        cursor.execute("""
            SELECT
                SkillName,
                SkillLevel,
                CategoryName
            FROM skills_repository
            WHERE EmployeeID = %s
            ORDER BY CategoryName, SkillName
        """, (employee_id,))
        skills = cursor.fetchall()

        cursor.execute("""
            SELECT
                RequestID,
                SkillName,
                SkillLevel,
                CategoryName,
                RequestStatus,
                DATE_FORMAT(RequestedAt, '%Y-%m-%d %H:%i') AS RequestedAt,
                DATE_FORMAT(ReviewedAt, '%Y-%m-%d %H:%i') AS ReviewedAt,
                ReviewedBy,
                RejectionReason
            FROM skill_requests
            WHERE EmployeeID = %s
            ORDER BY RequestedAt DESC
        """, (employee_id,))
        skill_requests = cursor.fetchall()

        cursor.execute("""
            SELECT
                MIN(TRIM(CategoryName)) AS CategoryName
            FROM skills_repository
            WHERE CategoryName IS NOT NULL
              AND TRIM(CategoryName) != ''
            GROUP BY LOWER(TRIM(CategoryName))
            ORDER BY CategoryName
        """)
        available_skill_categories = [
            row["CategoryName"]
            for row in cursor.fetchall()
            if row.get("CategoryName")
        ]

        cursor.execute("""
            SELECT
                CertificationName,
                CertificationStatus,
                CourseName
            FROM lms
            WHERE EmployeeID = %s
            ORDER BY CertificationName, CourseName
        """, (employee_id,))
        learning_records = cursor.fetchall()

        cursor.execute("""
            SELECT
                RequestID,
                CertificationName,
                CertificateLink,
                OriginalFileName,
                RequestStatus,
                DATE_FORMAT(RequestedAt, '%Y-%m-%d %H:%i') AS RequestedAt,
                DATE_FORMAT(ReviewedAt, '%Y-%m-%d %H:%i') AS ReviewedAt,
                ReviewedBy,
                RejectionReason
            FROM certification_requests
            WHERE EmployeeID = %s
            ORDER BY RequestedAt DESC
        """, (employee_id,))
        certification_requests = cursor.fetchall()

        cursor.execute("""
            SELECT ProjectName
            FROM project_history
            WHERE EmployeeID = %s
            ORDER BY ProjectName
        """, (employee_id,))
        project_history = cursor.fetchall()

        cursor.execute("""
            SELECT
                PerformanceRating,
                AwardsRecognition,
                PromotionHistory
            FROM performance_management
            WHERE EmployeeID = %s
            ORDER BY PerformanceRating DESC
            LIMIT 1
        """, (employee_id,))
        performance = cursor.fetchone() or {}

        if total_utilization == 0:
            utilization_status = "On Bench"
        elif total_utilization < 100:
            utilization_status = "Under Utilized"
        elif total_utilization == 100:
            utilization_status = "Utilized"
        else:
            utilization_status = "Over Utilized"

        name_parts = (employee.get("EmployeeName") or "").split()
        employee["Initials"] = "".join(
            part[0] for part in name_parts[:2] if part
        ).upper() or "E"

        return render_template(
            "employee_dashboard.html",
            employee=employee,
            active_projects=active_projects,
            total_utilization=round(total_utilization, 2),
            utilization_status=utilization_status,
            skills=skills,
            learning_records=learning_records,
            project_history=project_history,
            performance=performance,
            skill_requests=skill_requests,
            available_skill_categories=available_skill_categories,
            certification_requests=certification_requests
        )

    except mysql.connector.Error as error:
        print("Employee dashboard database error:", error)
        flash("Unable to load employee details from the database.", "error")
        return redirect(url_for("login"))

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()




@app.route("/request-skill", methods=["POST"])
def request_skill():
    if "user_email" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    if session.get("role") not in ["EMPLOYEE", "L&D", "LND"]:
        flash("Only employees can submit skill requests.", "error")
        return redirect(url_for("login"))

    skill_name = request.form.get("skill_name", "").strip()
    skill_level = request.form.get("skill_level", "").strip()
    category_name = request.form.get("category_name", "").strip()

    if not skill_name or not skill_level or not category_name:
        flash("Please enter the skill, level, and category.", "error")
        return redirect(url_for("employee_dashboard"))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT EmployeeID
            FROM hrms_data
            WHERE LOWER(TRIM(Email)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (session["user_email"],))
        employee = cursor.fetchone()

        if not employee:
            flash("Employee record was not found.", "error")
            return redirect(url_for("employee_dashboard"))

        employee_id = employee["EmployeeID"]

        cursor.execute("""
            SELECT EmployeeID
            FROM skills_repository
            WHERE EmployeeID = %s
              AND LOWER(TRIM(SkillName)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (employee_id, skill_name))

        if cursor.fetchone():
            flash("This skill is already present in your profile.", "error")
            return redirect(url_for("employee_dashboard"))

        cursor.execute("""
            SELECT RequestID
            FROM skill_requests
            WHERE EmployeeID = %s
              AND LOWER(TRIM(SkillName)) = LOWER(TRIM(%s))
              AND RequestStatus = 'Pending'
            LIMIT 1
        """, (employee_id, skill_name))

        if cursor.fetchone():
            flash("A pending request already exists for this skill.", "error")
            return redirect(url_for("employee_dashboard"))

        cursor.execute("""
            SELECT TRIM(CategoryName) AS CategoryName
            FROM skills_repository
            WHERE CategoryName IS NOT NULL
              AND TRIM(CategoryName) != ''
              AND LOWER(TRIM(CategoryName)) = LOWER(TRIM(%s))
            GROUP BY TRIM(CategoryName)
            ORDER BY COUNT(*) DESC, TRIM(CategoryName)
            LIMIT 1
        """, (category_name,))
        matching_category = cursor.fetchone()

        if not matching_category:
            flash(
                "Please select a valid existing skill category.",
                "error"
            )
            return redirect(url_for("employee_dashboard"))

        category_name = matching_category["CategoryName"]

        cursor.execute("""
            INSERT INTO skill_requests (
                EmployeeID,
                SkillName,
                SkillLevel,
                CategoryName,
                RequestStatus
            )
            VALUES (%s, %s, %s, %s, 'Pending')
        """, (employee_id, skill_name, skill_level, category_name))

        conn.commit()
        flash("Skill request sent to L&D for approval.", "success")

    except mysql.connector.Error as error:
        if conn:
            conn.rollback()
        print("Skill request error:", error)
        flash("Unable to submit the skill request.", "error")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

    return redirect(url_for("employee_dashboard"))


@app.route("/lnd-dashboard")
def lnd_dashboard():
    if "user_email" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    if session.get("role") not in ["L&D", "LND"]:
        flash("You do not have permission to access the L&D page.", "error")
        return redirect(url_for("login"))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # -------------------------------------------------------------
        # L&D KPI SUMMARY
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT COUNT(*) AS TotalCertifications
            FROM lms
            WHERE CertificationName IS NOT NULL
              AND TRIM(CertificationName) != ''
        """)
        total_certifications = cursor.fetchone()["TotalCertifications"] or 0

        cursor.execute("""
            SELECT COUNT(DISTINCT EmployeeID) AS CertifiedEmployees
            FROM lms
            WHERE CertificationName IS NOT NULL
              AND TRIM(CertificationName) != ''
              AND (
                    CertificationStatus IS NULL
                    OR TRIM(CertificationStatus) = ''
                    OR LOWER(TRIM(CertificationStatus)) IN (
                        'completed',
                        'certified',
                        'active'
                    )
              )
        """)
        certified_employees = cursor.fetchone()["CertifiedEmployees"] or 0

        cursor.execute("""
            SELECT COUNT(DISTINCT LOWER(TRIM(SkillName))) AS TotalSkills
            FROM skills_repository
            WHERE SkillName IS NOT NULL
              AND TRIM(SkillName) != ''
        """)
        total_skills = cursor.fetchone()["TotalSkills"] or 0

        cursor.execute("""
            SELECT COUNT(*) AS TotalEmployees
            FROM hrms_data
        """)
        total_employees = cursor.fetchone()["TotalEmployees"] or 0

        # -------------------------------------------------------------
        # SKILL REQUESTS
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT
                sr.RequestID,
                sr.EmployeeID,
                h.EmployeeName,
                h.Department,
                sr.SkillName,
                sr.SkillLevel,
                sr.CategoryName,
                sr.RequestStatus,
                DATE_FORMAT(sr.RequestedAt, '%Y-%m-%d %H:%i') AS RequestedAt,
                DATE_FORMAT(sr.ReviewedAt, '%Y-%m-%d %H:%i') AS ReviewedAt,
                sr.ReviewedBy,
                sr.RejectionReason
            FROM skill_requests sr
            LEFT JOIN hrms_data h
                ON sr.EmployeeID = h.EmployeeID
            ORDER BY
                CASE WHEN sr.RequestStatus = 'Pending' THEN 0 ELSE 1 END,
                sr.RequestedAt DESC
        """)
        requests_data = cursor.fetchall()

        skill_pending_count = sum(
            1 for item in requests_data
            if item.get("RequestStatus") == "Pending"
        )

        # -------------------------------------------------------------
        # CERTIFICATION REQUESTS
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT
                cr.RequestID,
                cr.EmployeeID,
                h.EmployeeName,
                h.Department,
                cr.CertificationName,
                cr.CertificateLink,
                cr.OriginalFileName,
                cr.RequestStatus,
                DATE_FORMAT(cr.RequestedAt, '%Y-%m-%d %H:%i') AS RequestedAt,
                DATE_FORMAT(cr.ReviewedAt, '%Y-%m-%d %H:%i') AS ReviewedAt,
                cr.ReviewedBy,
                cr.RejectionReason
            FROM certification_requests cr
            LEFT JOIN hrms_data h
                ON cr.EmployeeID = h.EmployeeID
            ORDER BY
                CASE WHEN cr.RequestStatus = 'Pending' THEN 0 ELSE 1 END,
                cr.RequestedAt DESC
        """)
        certification_requests_data = cursor.fetchall()

        certification_pending_count = sum(
            1 for item in certification_requests_data
            if item.get("RequestStatus") == "Pending"
        )

        pending_count = skill_pending_count + certification_pending_count

        # -------------------------------------------------------------
        # TOP SKILLS
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT
                MIN(TRIM(SkillName)) AS SkillName,
                COUNT(DISTINCT EmployeeID) AS EmployeeCount
            FROM skills_repository
            WHERE SkillName IS NOT NULL
              AND TRIM(SkillName) != ''
            GROUP BY LOWER(TRIM(SkillName))
            ORDER BY EmployeeCount DESC, SkillName
            LIMIT 5
        """)
        top_skills = cursor.fetchall()

        max_skill_count = max(
            [row["EmployeeCount"] or 0 for row in top_skills],
            default=1
        )

        for row in top_skills:
            row["BarPercent"] = round(
                ((row["EmployeeCount"] or 0) / max_skill_count) * 100,
                2
            )

        # -------------------------------------------------------------
        # TOP CERTIFICATIONS
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT
                MIN(TRIM(CertificationName)) AS CertificationName,
                COUNT(DISTINCT EmployeeID) AS EmployeeCount
            FROM lms
            WHERE CertificationName IS NOT NULL
              AND TRIM(CertificationName) != ''
            GROUP BY LOWER(TRIM(CertificationName))
            ORDER BY EmployeeCount DESC, CertificationName
            LIMIT 5
        """)
        top_certifications = cursor.fetchall()

        max_certification_count = max(
            [row["EmployeeCount"] or 0 for row in top_certifications],
            default=1
        )

        for row in top_certifications:
            row["BarPercent"] = round(
                ((row["EmployeeCount"] or 0) / max_certification_count) * 100,
                2
            )

        # -------------------------------------------------------------
        # PEOPLE COUNT BY SKILL AND PROFICIENCY
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT
                MIN(TRIM(SkillName)) AS SkillName,
                COUNT(DISTINCT CASE
                    WHEN LOWER(TRIM(SkillLevel)) = 'beginner'
                    THEN EmployeeID
                END) AS BeginnerCount,
                COUNT(DISTINCT CASE
                    WHEN LOWER(TRIM(SkillLevel)) = 'intermediate'
                    THEN EmployeeID
                END) AS IntermediateCount,
                COUNT(DISTINCT CASE
                    WHEN LOWER(TRIM(SkillLevel)) = 'advanced'
                    THEN EmployeeID
                END) AS AdvancedCount,
                COUNT(DISTINCT CASE
                    WHEN LOWER(TRIM(SkillLevel)) = 'expert'
                    THEN EmployeeID
                END) AS ExpertCount,
                COUNT(DISTINCT EmployeeID) AS TotalCount
            FROM skills_repository
            WHERE SkillName IS NOT NULL
              AND TRIM(SkillName) != ''
            GROUP BY LOWER(TRIM(SkillName))
            ORDER BY TotalCount DESC, SkillName
            LIMIT 12
        """)
        skill_proficiency_data = cursor.fetchall()

        # -------------------------------------------------------------
        # CERTIFICATION STATUS SUMMARY
        # -------------------------------------------------------------
        cursor.execute("""
            SELECT
                SUM(
                    CASE
                        WHEN LOWER(TRIM(CertificationStatus)) IN (
                            'completed',
                            'certified',
                            'active'
                        )
                        THEN 1 ELSE 0
                    END
                ) AS CompletedCount,
                SUM(
                    CASE
                        WHEN LOWER(TRIM(CertificationStatus)) = 'pending'
                        THEN 1 ELSE 0
                    END
                ) AS PendingCount,
                SUM(
                    CASE
                        WHEN LOWER(TRIM(CertificationStatus)) IN (
                            'expired',
                            'inactive'
                        )
                        THEN 1 ELSE 0
                    END
                ) AS ExpiredCount
            FROM lms
            WHERE CertificationName IS NOT NULL
              AND TRIM(CertificationName) != ''
        """)
        certification_summary = cursor.fetchone() or {}

        certification_summary["CompletedCount"] = (
            certification_summary.get("CompletedCount") or 0
        )
        certification_summary["PendingCount"] = (
            certification_summary.get("PendingCount") or 0
        )
        certification_summary["ExpiredCount"] = (
            certification_summary.get("ExpiredCount") or 0
        )

        return render_template(
            "lnd_dashboard.html",
            total_certifications=total_certifications,
            certified_employees=certified_employees,
            total_skills=total_skills,
            total_employees=total_employees,
            pending_count=pending_count,
            skill_pending_count=skill_pending_count,
            certification_pending_count=certification_pending_count,
            requests_data=requests_data,
            certification_requests_data=certification_requests_data,
            top_skills=top_skills,
            top_certifications=top_certifications,
            skill_proficiency_data=skill_proficiency_data,
            certification_summary=certification_summary
        )

    except mysql.connector.Error as error:
        print("L&D dashboard error:", error)
        flash("Unable to load the L&D dashboard.", "error")
        return redirect(url_for("login"))

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


@app.route("/skill-request/<int:request_id>/approve", methods=["POST"])
def approve_skill_request(request_id):
    if "user_email" not in session or session.get("role") not in ["L&D", "LND"]:
        flash("Only L&D can approve skill requests.", "error")
        return redirect(url_for("login"))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                EmployeeID,
                SkillName,
                SkillLevel,
                CategoryName,
                RequestStatus
            FROM skill_requests
            WHERE RequestID = %s
            FOR UPDATE
        """, (request_id,))
        skill_request = cursor.fetchone()

        if not skill_request:
            flash("Skill request was not found.", "error")
            return redirect(url_for("lnd_dashboard"))

        if skill_request["RequestStatus"] != "Pending":
            flash("This request has already been reviewed.", "error")
            return redirect(url_for("lnd_dashboard"))

        skill_name = (skill_request.get("SkillName") or "").strip()
        skill_level = (skill_request.get("SkillLevel") or "").strip()
        typed_category = (skill_request.get("CategoryName") or "").strip()

        if not skill_name or not skill_level or not typed_category:
            flash("The skill request contains incomplete information.", "error")
            return redirect(url_for("lnd_dashboard"))

        cursor.execute("""
            SELECT TRIM(CategoryName) AS CategoryName
            FROM skills_repository
            WHERE CategoryName IS NOT NULL
              AND TRIM(CategoryName) != ''
              AND LOWER(TRIM(CategoryName)) = LOWER(TRIM(%s))
            GROUP BY TRIM(CategoryName)
            ORDER BY COUNT(*) DESC, TRIM(CategoryName)
            LIMIT 1
        """, (typed_category,))
        existing_category = cursor.fetchone()

        if not existing_category:
            flash(
                "The selected category is no longer valid. "
                "Please reject the request and ask the employee to resubmit.",
                "error"
            )
            return redirect(url_for("lnd_dashboard"))

        category_name = existing_category["CategoryName"]

        cursor.execute("""
            SELECT TRIM(SkillName) AS SkillName
            FROM skills_repository
            WHERE SkillName IS NOT NULL
              AND TRIM(SkillName) != ''
              AND LOWER(TRIM(SkillName)) = LOWER(TRIM(%s))
            GROUP BY TRIM(SkillName)
            ORDER BY COUNT(*) DESC, TRIM(SkillName)
            LIMIT 1
        """, (skill_name,))
        existing_skill_name = cursor.fetchone()

        if existing_skill_name:
            skill_name = existing_skill_name["SkillName"]

        cursor.execute("""
            SELECT EmployeeID
            FROM skills_repository
            WHERE EmployeeID = %s
              AND LOWER(TRIM(SkillName)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (
            skill_request["EmployeeID"],
            skill_name
        ))

        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO skills_repository (
                    EmployeeID,
                    SkillName,
                    SkillCategory,
                    SkillLevel,
                    LastUpdatedDate,
                    CategoryName
                )
                VALUES (%s, %s, %s, %s, CURDATE(), %s)
            """, (
                skill_request["EmployeeID"],
                skill_name,
                category_name,
                skill_level,
                category_name
            ))

        cursor.execute("""
            UPDATE skill_requests
            SET
                RequestStatus = 'Approved',
                ReviewedAt = NOW(),
                ReviewedBy = %s,
                RejectionReason = NULL
            WHERE RequestID = %s
        """, (session["user_email"], request_id))

        conn.commit()
        flash("Skill request approved and added to the employee profile.", "success")

    except mysql.connector.Error as error:
        if conn:
            conn.rollback()
        print("Approve skill error:", error)
        flash("Unable to approve the skill request.", "error")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

    return redirect(url_for("lnd_dashboard"))


@app.route("/skill-request/<int:request_id>/reject", methods=["POST"])
def reject_skill_request(request_id):
    if "user_email" not in session or session.get("role") not in ["L&D", "LND"]:
        flash("Only L&D can reject skill requests.", "error")
        return redirect(url_for("login"))

    reason = request.form.get("rejection_reason", "").strip()

    if not reason:
        flash("Please enter a reason for rejection.", "error")
        return redirect(url_for("lnd_dashboard"))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            UPDATE skill_requests
            SET
                RequestStatus = 'Rejected',
                ReviewedAt = NOW(),
                ReviewedBy = %s,
                RejectionReason = %s
            WHERE RequestID = %s
              AND RequestStatus = 'Pending'
        """, (session["user_email"], reason, request_id))

        if cursor.rowcount == 0:
            flash("The request was not found or was already reviewed.", "error")
        else:
            conn.commit()
            flash("Skill request rejected.", "success")

    except mysql.connector.Error as error:
        if conn:
            conn.rollback()
        print("Reject skill error:", error)
        flash("Unable to reject the skill request.", "error")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

    return redirect(url_for("lnd_dashboard"))



@app.route("/request-certification", methods=["POST"])
def request_certification():
    if "user_email" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    if session.get("role") not in ["EMPLOYEE", "L&D", "LND"]:
        flash("Only employees can submit certification requests.", "error")
        return redirect(url_for("login"))

    certification_name = request.form.get("certification_name", "").strip()
    certificate_link = request.form.get("certificate_link", "").strip()
    certificate_file = request.files.get("certificate_file")

    has_link = bool(certificate_link)
    has_file = bool(
        certificate_file
        and certificate_file.filename
        and certificate_file.filename.strip()
    )

    if not certification_name:
        flash("Please enter the certification name.", "error")
        return redirect(url_for("employee_dashboard"))

    if not has_link and not has_file:
        flash("Provide either a valid certificate link or an attachment.", "error")
        return redirect(url_for("employee_dashboard"))

    if has_link and not is_valid_http_url(certificate_link):
        flash("The certificate link must be a valid http:// or https:// URL.", "error")
        return redirect(url_for("employee_dashboard"))

    if has_file and not allowed_certificate_file(certificate_file.filename):
        flash("Only PDF, PNG, JPG, and JPEG attachments are allowed.", "error")
        return redirect(url_for("employee_dashboard"))

    conn = None
    cursor = None
    stored_file_name = None
    original_file_name = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT EmployeeID
            FROM hrms_data
            WHERE LOWER(TRIM(Email)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (session["user_email"],))
        employee = cursor.fetchone()

        if not employee:
            flash("Employee record was not found.", "error")
            return redirect(url_for("employee_dashboard"))

        employee_id = employee["EmployeeID"]

        cursor.execute("""
            SELECT EmployeeID
            FROM lms
            WHERE EmployeeID = %s
              AND LOWER(TRIM(CertificationName)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (employee_id, certification_name))
        if cursor.fetchone():
            flash("This certification is already available in your profile.", "error")
            return redirect(url_for("employee_dashboard"))

        cursor.execute("""
            SELECT RequestID
            FROM certification_requests
            WHERE EmployeeID = %s
              AND LOWER(TRIM(CertificationName)) = LOWER(TRIM(%s))
              AND RequestStatus = 'Pending'
            LIMIT 1
        """, (employee_id, certification_name))
        if cursor.fetchone():
            flash("A pending request already exists for this certification.", "error")
            return redirect(url_for("employee_dashboard"))

        if has_file:
            original_file_name = secure_filename(certificate_file.filename)
            extension = original_file_name.rsplit(".", 1)[1].lower()
            stored_file_name = f"{employee_id}_{uuid.uuid4().hex}.{extension}"
            certificate_file.save(
                os.path.join(CERTIFICATE_UPLOAD_FOLDER, stored_file_name)
            )

        cursor.execute("""
            INSERT INTO certification_requests (
                EmployeeID,
                CertificationName,
                CertificateLink,
                StoredFileName,
                OriginalFileName,
                RequestStatus
            )
            VALUES (%s, %s, %s, %s, %s, 'Pending')
        """, (
            employee_id,
            certification_name,
            certificate_link or None,
            stored_file_name,
            original_file_name
        ))

        conn.commit()
        flash("Certification request sent to L&D for approval.", "success")

    except mysql.connector.Error as error:
        if conn:
            conn.rollback()

        if stored_file_name:
            saved_path = os.path.join(CERTIFICATE_UPLOAD_FOLDER, stored_file_name)
            if os.path.exists(saved_path):
                os.remove(saved_path)

        print("Certification request error:", error)
        flash("Unable to submit the certification request.", "error")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

    return redirect(url_for("employee_dashboard"))


@app.route("/certificate-attachment/<int:request_id>")
def view_certificate_attachment(request_id):
    if "user_email" not in session:
        return redirect(url_for("login"))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                cr.EmployeeID,
                cr.StoredFileName,
                cr.OriginalFileName,
                h.Email
            FROM certification_requests cr
            LEFT JOIN hrms_data h ON cr.EmployeeID = h.EmployeeID
            WHERE cr.RequestID = %s
            LIMIT 1
        """, (request_id,))
        item = cursor.fetchone()

        if not item or not item.get("StoredFileName"):
            abort(404)

        is_lnd = session.get("role") in ["L&D", "LND"]
        is_owner = (
            session.get("role") in ["EMPLOYEE", "L&D", "LND"]
            and (item.get("Email") or "").strip().lower()
            == session.get("user_email", "").strip().lower()
        )

        if not is_lnd and not is_owner:
            abort(403)

        return send_from_directory(
            CERTIFICATE_UPLOAD_FOLDER,
            item["StoredFileName"],
            as_attachment=False,
            download_name=item.get("OriginalFileName")
        )

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


@app.route("/certification-request/<int:request_id>/approve", methods=["POST"])
def approve_certification_request(request_id):
    if "user_email" not in session or session.get("role") not in ["L&D", "LND"]:
        flash("Only L&D can approve certification requests.", "error")
        return redirect(url_for("login"))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT EmployeeID, CertificationName, RequestStatus
            FROM certification_requests
            WHERE RequestID = %s
            FOR UPDATE
        """, (request_id,))
        cert_request = cursor.fetchone()

        if not cert_request:
            flash("Certification request was not found.", "error")
            return redirect(url_for("lnd_dashboard"))

        if cert_request["RequestStatus"] != "Pending":
            flash("This certification request has already been reviewed.", "error")
            return redirect(url_for("lnd_dashboard"))

        cursor.execute("""
            SELECT EmployeeID
            FROM lms
            WHERE EmployeeID = %s
              AND LOWER(TRIM(CertificationName)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (
            cert_request["EmployeeID"],
            cert_request["CertificationName"]
        ))

        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO lms (
                    EmployeeID,
                    CertificationName,
                    CertificationStatus
                )
                VALUES (%s, %s, 'Completed')
            """, (
                cert_request["EmployeeID"],
                cert_request["CertificationName"].strip()
            ))

        cursor.execute("""
            UPDATE certification_requests
            SET
                RequestStatus = 'Approved',
                ReviewedAt = NOW(),
                ReviewedBy = %s,
                RejectionReason = NULL
            WHERE RequestID = %s
        """, (session["user_email"], request_id))

        conn.commit()
        flash("Certification approved and added to the employee profile.", "success")

    except mysql.connector.Error as error:
        if conn:
            conn.rollback()
        print("Approve certification error:", error)
        flash("Unable to approve the certification request.", "error")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

    return redirect(url_for("lnd_dashboard"))


@app.route("/certification-request/<int:request_id>/reject", methods=["POST"])
def reject_certification_request(request_id):
    if "user_email" not in session or session.get("role") not in ["L&D", "LND"]:
        flash("Only L&D can reject certification requests.", "error")
        return redirect(url_for("login"))

    reason = request.form.get("rejection_reason", "").strip()

    if not reason:
        flash("Please enter a rejection reason.", "error")
        return redirect(url_for("lnd_dashboard"))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            UPDATE certification_requests
            SET
                RequestStatus = 'Rejected',
                ReviewedAt = NOW(),
                ReviewedBy = %s,
                RejectionReason = %s
            WHERE RequestID = %s
              AND RequestStatus = 'Pending'
        """, (
            session["user_email"],
            reason,
            request_id
        ))

        if cursor.rowcount == 0:
            flash("The request was not found or was already reviewed.", "error")
        else:
            conn.commit()
            flash("Certification request rejected.", "success")

    except mysql.connector.Error as error:
        if conn:
            conn.rollback()
        print("Reject certification error:", error)
        flash("Unable to reject the certification request.", "error")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

    return redirect(url_for("lnd_dashboard"))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for("login"))

if __name__ == "__main__":
    # Render starts this application with Gunicorn. This block is for localhost.
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )