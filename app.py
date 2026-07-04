from flask import Flask, render_template, request
import mysql.connector

app = Flask(__name__)


def get_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="LESNAR@1p",
        database="employee360"
    )


@app.route("/")
def dashboard():
    search = request.args.get("search", "")
    status = request.args.get("status", "All")
    department = request.args.get("department", "All")
    skill = request.args.get("skill", "")

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
    # Important fix:
    # Allocation is calculated in the alloc subquery first.
    # This prevents wrong 300% / 900% values caused by joins with skills/lms/history.
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

            GROUP_CONCAT(DISTINCT
                CASE
                    WHEN p.ProjectStatus = 'In Progress'
                    THEN CONCAT(
                        COALESCE(p.ProjectName, ''), '|',
                        COALESCE(p.EmployeeRole, ''), '|',
                        COALESCE(p.AllocationPercentage, 0), '|',
                        COALESCE(p.BillableStatus, '')
                    )
                END
                SEPARATOR ';;'
            ) AS ActiveProjects,

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

    filtered_employees = []

    for emp in employees:
        utilization = emp["AllocationPercentage"] or 0

        active_projects = []

        if emp.get("ActiveProjects"):
            project_items = emp["ActiveProjects"].split(";;")

            for item in project_items:
                parts = item.split("|")

                if len(parts) == 4:
                    active_projects.append({
                        "ProjectName": parts[0],
                        "EmployeeRole": parts[1],
                        "AllocationPercentage": int(float(parts[2])),
                        "BillableStatus": parts[3]
                    })

        emp["ActiveProjectList"] = active_projects

        if utilization == 0:
            emp["Status"] = "On Bench"
        elif utilization > 100:
            emp["Status"] = "Overallocated"
        else:
            emp["Status"] = "Allocated"

        name_parts = emp["EmployeeName"].split()
        emp["Initials"] = "".join([part[0] for part in name_parts[:2]]).upper()

        emp["SkillList"] = emp["Skills"].split(", ") if emp["Skills"] else []
        emp["CertificationList"] = emp["Certifications"].split(", ") if emp["Certifications"] else []
        emp["CourseList"] = emp["Courses"].split(", ") if emp["Courses"] else []
        emp["ProjectHistoryList"] = emp["ProjectHistory"].split(", ") if emp["ProjectHistory"] else []

        if status == "All" or emp["Status"] == status:
            filtered_employees.append(emp)

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
        total_records=len(filtered_employees)
    )


if __name__ == "__main__":
    app.run(debug=True)
