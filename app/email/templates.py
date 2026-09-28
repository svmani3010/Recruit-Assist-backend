


from datetime import datetime, timedelta
from html import escape
from typing import Tuple
from urllib.parse import urlparse


class EmailTemplates:
    """Email templates for HR notifications"""
    
    # PNG raster of company_logo_nobg_svg.svg — email clients do not reliably render SVG <img>.
    KOVETTED_LOGO_FILENAME = "company_logo_nobg_svg.png"

    @staticmethod
    def _video_mode_sub(data: dict) -> str:
        from app.services.calendar_identity import video_call_label

        return video_call_label(data)

    @staticmethod
    def _h(value) -> str:
        return escape(str(value or ""), quote=True)

    @staticmethod
    def _href(value) -> str:
        text = str(value or "").strip()
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return escape(text, quote=True)
        return ""

    @staticmethod
    def _frontend_base_url() -> str:
        try:
            from app.settings import get_settings

            return (get_settings().frontend_url or "").strip().rstrip("/")
        except Exception:
            return ""

    @classmethod
    def _kovetted_logo_url(cls) -> str:
        base = cls._frontend_base_url()
        return f"{base}/{cls.KOVETTED_LOGO_FILENAME}" if base else ""

    @classmethod
    def _kovetted_logo_html(
        cls,
        *,
        width: int = 150,
        height: int = 34,
        margin_bottom: int = 22,
        max_width: int = 170,
    ) -> str:
        logo_url = cls._kovetted_logo_url()
        if logo_url:
            return (
                f'<img src="{cls._h(logo_url)}" alt="KOVETTED" width="{width}" '
                f'style="display:block;margin:0 auto {margin_bottom}px auto;border:0;'
                f"height:{height}px;width:auto;max-width:{max_width}px;\" />"
            )
        return (
            '<div style="text-align:center;margin:0 0 22px 0;">'
            '<span style="font-family:Arial,Helvetica,sans-serif;font-size:20px;'
            'font-weight:800;letter-spacing:0.08em;color:#231F20;">KOVETTED</span></div>'
        )

    @classmethod
    def kyc_verification_otp_template(cls, otp: str) -> Tuple[str, str, str]:
        """Branded KYC OTP email with the standard Kovetted logo."""
        otp_safe = cls._h(otp)
        subject = "Recruit Assist - KYC Verification"
        logo_html = cls._kovetted_logo_html()
        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{cls._h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {logo_html}
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:480px;background-color:#ffffff;border-radius:16px;
                      border:1px solid #e8eef4;border-top:4px solid #00ADEE;">
          <tr>
            <td style="padding:32px 28px;font-family:Arial,Helvetica,sans-serif;">
              <h1 style="margin:0 0 12px 0;font-size:24px;line-height:1.3;font-weight:700;color:#231F20;text-align:center;">
                KYC Verification
              </h1>
              <p style="margin:0 0 18px 0;font-size:15px;line-height:1.55;color:#475569;text-align:center;">
                Use the verification code below to complete your recruiter account verification.
              </p>
              <div style="text-align:center;padding:18px 16px;background-color:#EAF6FC;border-radius:12px;
                          font-size:28px;font-weight:700;letter-spacing:0.2em;color:#231F20;">
                {otp_safe}
              </div>
              <p style="margin:18px 0 0 0;font-size:13px;line-height:1.5;color:#94a3b8;text-align:center;">
                This code expires in 5 minutes. If you did not request it, you can ignore this email.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
        text_body = f"Your Recruit Assist KYC verification code is: {otp}"
        return subject, html_body, text_body

    @classmethod
    def mobile_verified_confirmation_template(
        cls, name: str = "", phone_masked: str = ""
    ) -> Tuple[str, str, str]:
        """AC10 — email confirmation after mobile WhatsApp OTP verify (no SMS)."""
        subject = "Your mobile number is verified — Kovetted"
        logo_html = cls._kovetted_logo_html()
        name_safe = cls._h(name or "there")
        phone_safe = cls._h(phone_masked or "your mobile number")
        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{cls._h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {logo_html}
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:480px;background-color:#ffffff;border-radius:16px;
                      border:1px solid #e8eef4;border-top:4px solid #00ADEE;">
          <tr>
            <td style="padding:32px 28px;font-family:Arial,Helvetica,sans-serif;">
              <h1 style="margin:0 0 12px 0;font-size:24px;line-height:1.3;font-weight:700;color:#231F20;text-align:center;">
                Mobile number verified
              </h1>
              <p style="margin:0 0 18px 0;font-size:15px;line-height:1.55;color:#475569;text-align:center;">
                Hi {name_safe}, your number {phone_safe} has been verified successfully.
                You can continue setting up your Kovetted profile.
              </p>
              <p style="margin:18px 0 0 0;font-size:13px;line-height:1.5;color:#94a3b8;text-align:center;">
                If you did not verify this number, please contact support.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
        text_body = (
            f"Hi {name or 'there'}, your mobile number {phone_masked or ''} "
            "has been verified successfully on Kovetted."
        )
        return subject, html_body, text_body

    @staticmethod
    def _google_calendar_url(
        scheduled_date: str,
        scheduled_time: str,
        *,
        duration_minutes: int = 60,
        timezone_name: str = "",
        title: str = "",
        details: str = "",
        location: str = "",
    ) -> str:
        from urllib.parse import quote

        from app.timezone_utils import localize, normalize_tz_name

        date_raw = (scheduled_date or "").strip()
        time_raw = (scheduled_time or "").strip()
        if not date_raw or not time_raw:
            return ""
        try:
            start_dt = datetime.strptime(f"{date_raw} {time_raw}", "%Y-%m-%d %I:%M %p")
            tz_name = normalize_tz_name(timezone_name)
            start_dt = localize(start_dt, tz_name)
            end_dt = start_dt + timedelta(minutes=int(duration_minutes or 60))
            dates = (
                f"{start_dt.strftime('%Y%m%dT%H%M%S')}/"
                f"{end_dt.strftime('%Y%m%dT%H%M%S')}"
            )
            return (
                "https://calendar.google.com/calendar/render?action=TEMPLATE"
                f"&text={quote(title or 'Interview')}"
                f"&dates={dates}"
                f"&ctz={quote(tz_name)}"
                f"&details={quote(details or '')}"
                f"&location={quote(location or '')}"
            )
        except Exception:
            return ""

    @staticmethod
    def _outlook_calendar_url(
        scheduled_date: str,
        scheduled_time: str,
        *,
        duration_minutes: int = 60,
        timezone_name: str = "",
        title: str = "",
        details: str = "",
        location: str = "",
    ) -> str:
        from urllib.parse import quote

        from app.timezone_utils import localize, normalize_tz_name

        date_raw = (scheduled_date or "").strip()
        time_raw = (scheduled_time or "").strip()
        if not date_raw or not time_raw:
            return ""
        try:
            start_dt = datetime.strptime(f"{date_raw} {time_raw}", "%Y-%m-%d %I:%M %p")
            tz_name = normalize_tz_name(timezone_name)
            start_dt = localize(start_dt, tz_name)
            end_dt = start_dt + timedelta(minutes=int(duration_minutes or 60))
            return (
                "https://outlook.office.com/calendar/0/deeplink/compose?"
                f"subject={quote(title or 'Interview')}"
                f"&startdt={quote(start_dt.strftime('%Y-%m-%dT%H:%M:%S'))}"
                f"&enddt={quote(end_dt.strftime('%Y-%m-%dT%H:%M:%S'))}"
                f"&body={quote(details or '')}"
                f"&location={quote(location or '')}"
            )
        except Exception:
            return ""

    @staticmethod
    def _add_to_calendar_url(
        data: dict,
        *,
        duration_minutes: int = 60,
        timezone_name: str = "",
        title: str = "",
        details: str = "",
        location: str = "",
    ) -> str:
        from app.services.calendar_identity import is_microsoft_meeting

        date_raw = (data.get("scheduled_date") or "").strip()
        time_raw = (data.get("scheduled_time") or "").strip()
        if is_microsoft_meeting(data):
            outlook = EmailTemplates._outlook_calendar_url(
                date_raw,
                time_raw,
                duration_minutes=duration_minutes,
                timezone_name=timezone_name,
                title=title,
                details=details,
                location=location,
            )
            return outlook or (data.get("meeting_link") or data.get("maps_url") or "").strip()
        return EmailTemplates._google_calendar_url(
            date_raw,
            time_raw,
            duration_minutes=duration_minutes,
            timezone_name=timezone_name,
            title=title,
            details=details,
            location=location,
        )
    
    @staticmethod
    def _get_base_template(title: str, content: str, job_role: str = None) -> str:
        """Base HTML template for emails"""
        safe_title = EmailTemplates._h(title)
        safe_role = EmailTemplates._h(job_role) if job_role and job_role != "N/A" else ""
        header_title = f"{safe_title} - {safe_role}" if safe_role else safe_title
        logo_html = EmailTemplates._kovetted_logo_html(margin_bottom=16)
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    color: #333;
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 20px;
                }}
                .header {{
                    background-color: #4CAF50;
                    color: white;
                    padding: 20px;
                    text-align: center;
                    border-radius: 5px 5px 0 0;
                }}
                .content {{
                    background-color: #f9f9f9;
                    padding: 20px;
                    border: 1px solid #ddd;
                    border-radius: 0 0 5px 5px;
                }}
                .info-box {{
                    background-color: white;
                    padding: 15px;
                    margin: 10px 0;
                    border-left: 4px solid #4CAF50;
                }}
                .label {{
                    font-weight: bold;
                    color: #000;
                }}
                .footer {{
                    margin-top: 20px;
                    padding-top: 20px;
                    border-top: 1px solid #ddd;
                    font-size: 12px;
                    color: #777;
                    text-align: center;
                }}
                .button {{
                    display: inline-block;
                    padding: 10px 20px;
                    background-color: #4CAF50;
                    color: white;
                    text-decoration: none;
                    border-radius: 5px;
                    margin: 10px 0;
                }}
            </style>
        </head>
        <body>
            <div style="padding:20px 20px 0;text-align:center;">
                {logo_html}
            </div>
            <div class="header">
                <h2>{header_title}</h2>
            </div>
            <div class="content">
                {content}
            </div>
            <div class="footer">
                <p>This is an automated notification from Screening Assist System</p>
                <p>Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
            </div>
        </body>
        </html>
        """

    @staticmethod
    def _l2_pdf_note(data: dict) -> Tuple[str, str]:
        if data.get("is_candidate") or not data.get("l2_pdf_attached"):
            return "", ""
        html = (
            '<div class="info-box">'
            "<p><strong>L2 interview questions (PDF attached)</strong></p>"
            "<p>Use this as the question reference during the interview. "
            "This file is for HR and interviewers only — do not share it with the candidate.</p>"
            "</div>"
        )
        text = (
            "\nL2 interview questions are attached as a PDF. "
            "For HR and interviewers only — do not share with the candidate.\n"
        )
        return html, text
    
    def get_template(self, action_type: str, data: dict) -> Tuple[str, str, str]:
        """
        Get email template based on action type
        
        Args:
            action_type: Type of action
            data: Data for template
        
        Returns:
            Tuple of (subject, html_body, text_body)
        """
        template_map = {
            'resume_upload': self._resume_upload_template,
            'resume_upload_hr': self._resume_upload_hr_template,
            'status_update': self._status_update_template,
            'job_posted': self._job_posted_template,
            'job_updated': self._job_updated_template,
            'initial_screening_complete': self._initial_screening_complete_template,
            'final_screening_complete': self._final_screening_complete_template,
            'l1_interview_complete': self._l1_interview_complete_template,
            'l2_questions_generated': self._l2_questions_generated_template,
            'onboarding_complete': self._onboarding_complete_template,
            'screening_answers_submitted': self._screening_answers_submitted_template,
            'interview_schedule': self._interview_schedule_template,
            'interview_reschedule': self._interview_reschedule_template,
            'interview_confirmed': self._interview_confirmed_template,
            'interview_cancelled': self._interview_cancelled_template,
            'candidate_apply': self._candidate_apply_template,
            'candidate_rejected': self._candidate_rejected_template,
            'l1_passed': self._l1_passed_template,
        }
        
        template_func = template_map.get(action_type, self._default_template)
        return template_func(data)
    
    def _resume_upload_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for candidate resume upload"""
        candidate_name = data.get('candidate_name', 'N/A')
        candidate_email = data.get('candidate_email', 'N/A')
        job_role = data.get('job_role', 'N/A')
        job_id = data.get('job_id', 'N/A')
        candidate_id = data.get('candidate_id', 'N/A')
        
        subject = f"New Resume Uploaded - {candidate_name} for {job_role}"
        
        content = f"""
        <h3>New Candidate Application Received</h3>
        <div class="info-box">
            <p><span class="label">Candidate Name:</span> {candidate_name}</p>
            <p><span class="label">Email:</span> {candidate_email}</p>
            <p><span class="label">Job Role:</span> {job_role}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
            <p><span class="label">Candidate ID:</span> {candidate_id}</p>
        </div>
        <p>A new candidate has submitted their resume for the position. Initial screening is in progress.</p>
        """
        
        html_body = self._get_base_template("New Resume Uploaded", content, job_role)
        
        text_body = f"""
New Resume Uploaded

Candidate Name: {candidate_name}
Email: {candidate_email}
Job Role: {job_role}
Job ID: {job_id}
Candidate ID: {candidate_id}

A new candidate has submitted their resume for the position. Initial screening is in progress.
        """
        
        return subject, html_body, text_body
    
    def _resume_upload_hr_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for HR manual resume upload"""
        candidate_name = data.get('candidate_name', 'N/A')
        job_role = data.get('job_role', 'N/A')
        job_id = data.get('job_id', 'N/A')
        candidate_id = data.get('candidate_id', 'N/A')
        
        subject = f"Resume Manually Added by HR - {candidate_name} for {job_role}"
        
        content = f"""
        <h3>Resume Manually Added to System</h3>
        <div class="info-box">
            <p><span class="label">Candidate Name:</span> {candidate_name}</p>
            <p><span class="label">Job Role:</span> {job_role}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
            <p><span class="label">Candidate ID:</span> {candidate_id}</p>
            <p><span class="label">Added By:</span> HR</p>
        </div>
        <p>A resume has been manually added to the system by HR. Final screening is in progress.</p>
        """
        
        html_body = self._get_base_template("Resume Added by HR", content, job_role)
        
        text_body = f"""
Resume Manually Added by HR

Candidate Name: {candidate_name}
Job Role: {job_role}
Job ID: {job_id}
Candidate ID: {candidate_id}
Added By: HR

A resume has been manually added to the system by HR. Final screening is in progress.
        """
        
        return subject, html_body, text_body
    
    def _status_update_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for status / milestone updates (HR or candidate)."""
        candidate_id = data.get('candidate_id', 'N/A')
        job_id = data.get('job_id', 'N/A')
        status = data.get('status', 'N/A')
        candidate_name = data.get('candidate_name', 'Unknown')
        job_role = data.get('job_role', 'N/A')
        message = data.get('message') or data.get('body') or ''
        short_url = data.get('short_url') or data.get('frontend_url') or ''
        is_candidate = bool(data.get('is_candidate'))

        if is_candidate:
            subject = f"Kovetted update — {status}"
            link_html = (
                f'<p style="margin:18px 0 0 0;text-align:center;">'
                f'<a href="{self._href(short_url)}" style="color:#00ADEE;font-weight:700;">'
                f'Open in Kovetted</a></p>'
                if self._href(short_url)
                else ""
            )
            content = f"""
            <h3>{self._h(status)}</h3>
            <div class="info-box">
                <p>Hi {self._h(candidate_name)},</p>
                <p>{self._h(message or 'There is an update on your application.')}</p>
                <p><span class="label">Role:</span> {self._h(job_role)}</p>
            </div>
            {link_html}
            """
            html_body = self._get_base_template(str(status), content, job_role)
            text_body = (
                f"Hi {candidate_name},\n\n"
                f"{message or status}\n"
                f"Role: {job_role}\n"
                f"{('Open: ' + short_url) if short_url else ''}\n"
            )
            return subject, html_body, text_body

        subject = f"Candidate Status Updated - {candidate_name} - {status}"
        
        content = f"""
        <h3>Candidate Status Updated</h3>
        <div class="info-box">
            <p><span class="label">Candidate:</span> {candidate_name}</p>
            <p><span class="label">Candidate ID:</span> {candidate_id}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
            <p><span class="label">New Status:</span> <strong>{status}</strong></p>
        </div>
        <p>The candidate's status has been updated in the system.</p>
        """
        
        html_body = self._get_base_template("Status Updated", content, job_role)
        
        text_body = f"""
Candidate Status Updated

Candidate: {candidate_name}
Candidate ID: {candidate_id}
Job ID: {job_id}
New Status: {status}

The candidate's status has been updated in the system.
        """
        
        return subject, html_body, text_body
    
    def _job_posted_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for new job posting"""
        job_role = data.get('job_role', 'N/A')
        job_id = data.get('job_id', 'N/A')
        location = data.get('location', 'N/A')
        employment_type = data.get('employment_type', 'N/A')
        
        subject = f"New Job Posted - {job_role} ({job_id})"
        
        content = f"""
        <h3>New Job Position Created</h3>
        <div class="info-box">
            <p><span class="label">Job Role:</span> {job_role}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
            <p><span class="label">Location:</span> {location}</p>
            <p><span class="label">Employment Type:</span> {employment_type}</p>
        </div>
        <p>A new job position has been created and is now active in the system.</p>
        """
        
        html_body = self._get_base_template("New Job Posted", content, job_role)
        
        text_body = f"""
New Job Posted

Job Role: {job_role}
Job ID: {job_id}
Location: {location}
Employment Type: {employment_type}

A new job position has been created and is now active in the system.
        """
        
        return subject, html_body, text_body
    
    def _job_updated_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for job update"""
        job_role = data.get('job_role', 'N/A')
        job_id = data.get('job_id', 'N/A')
        
        subject = f"Job Updated - {job_role} ({job_id})"
        
        content = f"""
        <h3>Job Details Updated</h3>
        <div class="info-box">
            <p><span class="label">Job Role:</span> {job_role}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
        </div>
        <p>The job details have been updated in the system.</p>
        """
        
        html_body = self._get_base_template("Job Updated", content, job_role)
        
        text_body = f"""
Job Updated

Job Role: {job_role}
Job ID: {job_id}

The job details have been updated in the system.
        """
        
        return subject, html_body, text_body
    
    def _initial_screening_complete_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for initial screening completion"""
        job_role = data.get('job_role', 'N/A')
        subject = "Candidate Passed L0 Matching"
        
        content = """
        <p>Hi Team,</p>
        <p>The candidate’s resume has been successfully screened and approved for the next stage. The profile is now available for further review in the dashboard.</p>
        <p>Regards,<br>
        Recruit Assist<br>
        Fanisko</p>
        """
        
        html_body = self._get_base_template("Candidate Passed L0 Matching", content, job_role)
        
        text_body = """
Hi Team,

The candidate’s resume has been successfully screened and approved for the next stage. The profile is now available for further review in the dashboard.

Regards,
Recruit Assist
Fanisko
        """
        
        return subject, html_body, text_body
    
    def _final_screening_complete_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for final screening completion"""
        candidate_id = data.get('candidate_id', 'N/A')
        job_id = data.get('job_id', 'N/A')
        llm_score = data.get('llm_score', 'N/A')
        candidate_name = data.get('candidate_name', 'Unknown')
        
        subject = f"Final Screening Complete - {candidate_name} - Score: {llm_score}"
        
        content = f"""
        <h3>Final Screening Completed</h3>
        <div class="info-box">
            <p><span class="label">Candidate:</span> {candidate_name}</p>
            <p><span class="label">Candidate ID:</span> {candidate_id}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
            <p><span class="label">LLM Score:</span> <strong>{llm_score}</strong></p>
        </div>
        <p>The final screening process has been completed for this candidate.</p>
        """
        
        job_role = data.get('job_role', 'N/A')
        html_body = self._get_base_template("Final Screening Complete", content, job_role)
        
        text_body = f"""
Final Screening Complete

Candidate: {candidate_name}
Candidate ID: {candidate_id}
Job ID: {job_id}
LLM Score: {llm_score}

The final screening process has been completed for this candidate.
        """
        
        return subject, html_body, text_body
    
    def _l1_interview_complete_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for L1 interview completion"""
        # candidate_id = data.get('candidate_id', 'N/A')
        # job_id = data.get('job_id', 'N/A')
        candidate_name = data.get('candidate_name', 'Unknown')
        # overall_l1_score = data.get('overall_l1_score', 'N/A')
        
        is_candidate = data.get('is_candidate', False)
        
        if is_candidate:
            subject = f"L1 Interview Complete - {candidate_name}"
            content = f"""
            <p>Hi {candidate_name},</p>
            <p>Thank you for completing the Level 1 interview questions.</p>
            <p>Your responses have been successfully submitted and are currently under review by our team. We will update you once the evaluation is completed.</p>
            <p>Please keep an eye on your email for further communication.</p>
            <p>Best regards,<br>
            HR Team<br>
            Fanisko</p>
            """
        else:
            subject = "L1 Interview Completed by Candidate"
            content = """
            <p>Hi Team,</p>
            <p>The candidate has completed the Level 1 interview questions. Responses are now available for review in the system.</p>
            <p>Regards,<br>
            Recruit Assist<br>
            Fanisko</p>
            """
        
        job_role = data.get('job_role', 'N/A')
        html_body = self._get_base_template("L1 Interview Complete", content, job_role)
        
        if is_candidate:
            text_body = f"""
Hi {candidate_name},

Thank you for completing the Level 1 interview questions.

Your responses have been successfully submitted and are currently under review by our team. We will update you once the evaluation is completed.

Please keep an eye on your email for further communication.

Best regards,
HR Team
Fanisko
            """
        else:
            text_body = """
Hi Team,

The candidate has completed the Level 1 interview questions. Responses are now available for review in the system.

Regards,
Recruit Assist
Fanisko
            """
        
        return subject, html_body, text_body
    
    def _l2_questions_generated_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for L2 questions generation"""
        candidate_id = data.get('candidate_id', 'N/A')
        job_id = data.get('job_id', 'N/A')
        num_questions = data.get('num_questions', 'N/A')
        candidate_name = data.get('candidate_name', 'Unknown')
        
        subject = f"L2 Questions Generated - {candidate_name}"
        
        content = f"""
        <h3>L2 Interview Questions Generated</h3>
        <div class="info-box">
            <p><span class="label">Candidate:</span> {candidate_name}</p>
            <p><span class="label">Candidate ID:</span> {candidate_id}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
            <p><span class="label">Number of Questions:</span> {num_questions}</p>
        </div>
        <p>L2 interview questions have been generated and are ready for review.</p>
        """
        
        job_role = data.get('job_role', 'N/A')
        html_body = self._get_base_template("L2 Questions Generated", content, job_role)
        
        text_body = f"""
L2 Questions Generated

Candidate: {candidate_name}
Candidate ID: {candidate_id}
Job ID: {job_id}
Number of Questions: {num_questions}

L2 interview questions have been generated and are ready for review.
        """
        
        return subject, html_body, text_body
    
    def _onboarding_complete_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for onboarding completion"""
        candidate_id = data.get('candidate_id', 'N/A')
        job_id = data.get('job_id', 'N/A')
        candidate_name = data.get('candidate_name', 'Unknown')
        email = data.get('email', 'N/A')
        phone = data.get('phone', 'N/A')
        status = data.get('status', 'N/A')
        
        subject = f"Onboarding Complete - {candidate_name}"
        
        content = f"""
        <h3>Candidate Onboarding Completed</h3>
        <div class="info-box">
            <p><span class="label">Candidate:</span> {candidate_name}</p>
            <p><span class="label">Email:</span> {email}</p>
            <p><span class="label">Phone:</span> {phone}</p>
            <p><span class="label">Candidate ID:</span> {candidate_id}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
            <p><span class="label">Status:</span> <span style="color: green; font-weight: bold;">{status}</span></p>
        </div>
        <p>The candidate has completed their profile and onboarding information.</p>
        """
        
        job_role = data.get('job_role', 'N/A')
        html_body = self._get_base_template("Onboarding Complete", content, job_role)
        
        text_body = f"""
Onboarding Complete

Candidate: {candidate_name}
Email: {email}
Phone: {phone}
Candidate ID: {candidate_id}
Job ID: {job_id}
Status: {status}

The candidate has completed their profile and onboarding information.
        """
        
        return subject, html_body, text_body

    
    def _screening_answers_submitted_template(self, data: dict) -> Tuple[str, str, str]:
        """Template for screening answers submission"""
        candidate_id = data.get('candidate_id', 'N/A')
        job_id = data.get('job_id', 'N/A')
        candidate_name = data.get('candidate_name', 'Unknown')
        email = data.get('email', 'N/A')
        phone = data.get('phone', 'N/A')
        num_answers = data.get('num_answers', 0)
        
        subject = f"Screening Answers Submitted - {candidate_name}"
        
        content = f"""
        <h3>Candidate Screening Answers Submitted</h3>
        <div class="info-box">
            <p><span class="label">Candidate:</span> {candidate_name}</p>
            <p><span class="label">Email:</span> {email}</p>
            <p><span class="label">Phone:</span> {phone}</p>
            <p><span class="label">Candidate ID:</span> {candidate_id}</p>
            <p><span class="label">Job ID:</span> {job_id}</p>
            <p><span class="label">Questions Answered:</span> {num_answers}</p>
        </div>
        <p>The candidate has submitted their answers to the screening questions.</p>
        """
        
        job_role = data.get('job_role', 'N/A')
        html_body = self._get_base_template("Screening Answers Submitted", content, job_role)
        
        text_body = f"""
Screening Answers Submitted

Candidate: {candidate_name}
Email: {email}
Phone: {phone}
Candidate ID: {candidate_id}
Job ID: {job_id}
Questions Answered: {num_answers}

The candidate has submitted their answers to the screening questions.
        """
        
        return subject, html_body, text_body

    def _interview_schedule_template(self, data: dict) -> Tuple[str, str, str]:
        """Candidate invite card when HR sends the scheduling link."""
        h, href = self._h, self._href
        is_candidate = bool(data.get("is_candidate"))
        candidate_name_raw = (data.get("candidate_name") or "Candidate").strip() or "Candidate"
        job_role_raw = (data.get("job_role") or "N/A").strip() or "N/A"
        booking_url = (data.get("booking_url") or "").strip()
        interview_level_raw = (
            (data.get("interview_level") or "Level 2").strip() or "Level 2"
        )
        company_raw = (data.get("company_name") or "Company").strip() or "Company"
        duration = int(data.get("duration_minutes") or 60)
        in_person = str(data.get("interview_mode") or "").lower() in {
            "in_person",
            "in-person",
            "onsite",
        }
        type_raw = "In-person" if in_person else "Video Call"
        duration_raw = f"{duration} mins"

        if is_candidate:
            logo_html = self._kovetted_logo_html()

            booking_href = href(booking_url)
            cta_html = ""
            if booking_href:
                cta_html = f"""
                  <tr>
                    <td align="left" style="padding:8px 0 22px 0;">
                      <a href="{booking_href}"
                         style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                         font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;
                         text-decoration:none;padding:12px 20px;border-radius:10px;">
                        Schedule Interview
                      </a>
                    </td>
                  </tr>
            """
            else:
                cta_html = """
                  <tr>
                    <td style="padding:8px 0 22px 0;font-family:Arial,Helvetica,sans-serif;
                               font-size:14px;color:#b91c1c;">
                      Scheduling link unavailable — please contact the recruiter.
                    </td>
                  </tr>
                """

            def info_cell(label: str, value: str) -> str:
                return f"""
                  <td width="50%" valign="top" style="padding:6px;">
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                           style="background-color:#EAF6FC;border-radius:10px;">
                      <tr>
                        <td style="padding:14px 14px;font-family:Arial,Helvetica,sans-serif;">
                          <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;
                                      text-transform:uppercase;color:#64748b;margin:0 0 4px 0;">
                            {h(label)}
                          </div>
                          <div style="font-size:15px;font-weight:700;color:#231F20;line-height:1.35;">
                            {h(value)}
                          </div>
                        </td>
                      </tr>
                    </table>
                  </td>
                """

            subject = f"You're invited to interview - {job_role_raw}"
            body_copy = (
                f"Congratulations! You've successfully completed the Level 1 Screening. "
                f"Please choose a convenient time for your {interview_level_raw} Interview "
                f"using the button below."
            )

            html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {logo_html}
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:560px;background-color:#ffffff;border-radius:16px;
                      border-top:4px solid #00ADEE;border:1px solid #e8eef4;border-top:4px solid #00ADEE;">
          <tr>
            <td style="padding:28px 28px 8px 28px;font-family:Arial,Helvetica,sans-serif;">
              <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                          text-transform:uppercase;color:#00ADEE;margin:0 0 10px 0;">
                Next Steps
            </div>
              <h1 style="margin:0 0 16px 0;font-size:28px;line-height:1.25;font-weight:700;color:#231F20;">
                You're invited to <span style="color:#00ADEE;">interview</span>
              </h1>
              <p style="margin:0 0 8px 0;font-size:15px;line-height:1.5;color:#475569;">
                Hi {h(candidate_name_raw)},
              </p>
              <p style="margin:0 0 18px 0;font-size:15px;line-height:1.55;color:#475569;">
                {h(body_copy)}
              </p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {cta_html}
              </table>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  {info_cell("Company Name", company_raw)}
                  {info_cell("Role", job_role_raw)}
                </tr>
                <tr>
                  {info_cell("Type", type_raw)}
                  {info_cell("Duration", duration_raw)}
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 28px 24px 28px;">
              <div style="border-top:1px solid #eef2f7;padding-top:14px;
                          font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#94a3b8;
                          text-align:center;">
                Kovetted · Hiring Team
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

            text_body = (
                "NEXT STEPS\n"
                "You're invited to interview\n\n"
                f"Hi {candidate_name_raw},\n\n"
                f"{body_copy}\n\n"
                f"Schedule Interview: {booking_url or 'Contact recruiter'}\n\n"
                f"Company Name: {company_raw}\n"
                f"Role: {job_role_raw}\n"
                f"Type: {type_raw}\n"
                f"Duration: {duration_raw}\n"
            )
            return subject, html_body, text_body

        # HR / interviewer notification (keep existing functional copy + L2 note)
        candidate_name = data.get("candidate_name", "Unknown")
        job_role = data.get("job_role", "N/A")
        booking_url = data.get("booking_url") or "Scheduling link unavailable — contact the recruiter."
        booking_href = self._href(booking_url)
        l2_html, l2_text = self._l2_pdf_note(data)
        subject = "Candidate Approved — Scheduling Link Sent"
        content = f"""
            <p>Hi Team,</p>
            <p>The candidate has been approved. A self-scheduling link has been sent so they can pick an interview slot.</p>
            {f'<p><strong>Booking link:</strong> <a href="{booking_href}">{self._h(booking_url)}</a></p>' if booking_href else ''}
            {l2_html}
            <p>Regards,<br>
            Recruit Assist<br>
            Fanisko</p>
            """
        html_body = self._get_base_template(subject, content, job_role)
        text_body = f"""
Hi Team,

The candidate has been approved successfully in the dashboard. The profile will now move to the next stage of the hiring process.
{l2_text}
Regards,
Recruit Assist
Fanisko
            """
        return subject, html_body, text_body

    def _interview_reschedule_template(self, data: dict) -> Tuple[str, str, str]:
        """
        Reschedule emails:
        - Candidate after HR sets a new slot: card-only scheduling update
        - Candidate + booking link: pick a new slot
        - HR after website direct-reschedule: card-only successfully rescheduled
        """
        h, href = self._h, self._href
        is_candidate = bool(data.get("is_candidate"))
        is_fyi = bool(data.get("is_fyi"))
        reschedule_complete = bool(
            data.get("reschedule_complete")
            or (
                data.get("scheduled_date")
                and data.get("scheduled_time")
                and not data.get("booking_url")
            )
        )

        candidate_name_raw = (data.get("candidate_name") or "Candidate").strip() or "Candidate"
        job_role_raw = (data.get("job_role") or "N/A").strip() or "N/A"
        interview_level_raw = (
            (data.get("interview_level") or "Level 2").strip() or "Level 2"
        )
        company_raw = (data.get("company_name") or "Company").strip() or "Company"

        def _format_slot_fields():
            timezone_raw = (data.get("timezone") or "").strip()
            in_person = str(data.get("interview_mode") or "").lower() in {
                "in_person",
                "in-person",
                "onsite",
            }
            date_display_raw = (data.get("scheduled_date") or "").strip()
            try:
                parsed_date = datetime.strptime(date_display_raw, "%Y-%m-%d")
                date_display_raw = parsed_date.strftime("%A, %b %d")
            except Exception:
                pass

            start_raw = (data.get("scheduled_time") or "").strip()
            duration = int(data.get("duration_minutes") or 60)
            time_display_raw = start_raw or "—"
            try:
                start_dt = datetime.strptime(start_raw, "%I:%M %p")
                end_dt = start_dt + timedelta(minutes=duration)
                end_label = end_dt.strftime("%I:%M %p").lstrip("0")
                start_label = start_dt.strftime("%I:%M %p").lstrip("0")
                time_display_raw = f"{start_label} - {end_label}"
                if timezone_raw:
                    time_display_raw = f"{time_display_raw} ({timezone_raw})"
            except Exception:
                if timezone_raw and start_raw:
                    time_display_raw = f"{start_raw} ({timezone_raw})"

            if in_person:
                mode_title = "In-person"
                mode_sub = (data.get("location_address") or "").strip() or "Office"
            else:
                mode_title = "Video Call"
                mode_sub = self._video_mode_sub(data)

            return {
                "date_display_raw": date_display_raw or "—",
                "time_display_raw": time_display_raw,
                "mode_title": mode_title,
                "mode_sub": mode_sub,
                "duration": duration,
            }

        def _logo_html() -> str:
            return self._kovetted_logo_html()

        def _detail_row(label: str, value: str, sub: str = "") -> str:
            sub_html = (
                f'<div style="font-size:12px;color:#64748b;margin-top:2px;">{h(sub)}</div>'
                if sub
                else ""
            )
            return f"""
              <tr>
                <td style="padding:0 0 10px 0;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                         style="background-color:#EAF6FC;border-radius:10px;">
                    <tr>
                      <td style="padding:14px 16px;font-family:Arial,Helvetica,sans-serif;">
                        <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;
                                    text-transform:uppercase;color:#64748b;margin:0 0 4px 0;">
                          {h(label)}
            </div>
                        <div style="font-size:15px;font-weight:700;color:#231F20;line-height:1.35;">
                          {value}
                        </div>
                        {sub_html}
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            """

        def _details_url() -> str:
            try:
                from app.settings import get_settings

                base = (get_settings().frontend_url or "").strip().rstrip("/")
                job_id = str(data.get("job_id") or "").strip()
                if base and job_id:
                    return f"{base}/recruiters/pipeline/{job_id}/applicants"
                if base:
                    return f"{base}/recruiters/dashboard"
            except Exception:
                pass
            return ""

        def _calendar_url(slot: dict) -> str:
            return self._add_to_calendar_url(
                data,
                duration_minutes=int(slot["duration"] or 60),
                timezone_name=(data.get("timezone") or "").strip(),
                title=f"Interview: {job_role_raw}",
                details=(
                    f"{interview_level_raw} interview for {job_role_raw}. "
                    f"{data.get('meeting_link') or data.get('location_address') or ''}"
                ),
                location=(
                    (data.get("meeting_link") or data.get("location_address") or "").strip()
                ),
            )

        # Candidate card — new slot already booked
        if reschedule_complete and is_candidate:
            slot = _format_slot_fields()
            subject = f"Your Interview Has Been Rescheduled - {job_role_raw}"
            intro = (
                f"Your {interview_level_raw} interview has been rescheduled. "
                "Please review the updated interview details below."
            )
            calendar_href = href(_calendar_url(slot))
            cta_html = ""
            if calendar_href:
                cta_html = f"""
                  <tr>
                    <td align="left" style="padding:8px 0 22px 0;">
                      <a href="{calendar_href}"
                         style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                         font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;
                         text-decoration:none;padding:12px 20px;border-radius:10px;">
                        Add to Calendar
                      </a>
                    </td>
                  </tr>
                """

            html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {_logo_html()}
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:560px;background-color:#ffffff;border-radius:16px;
                      border:1px solid #e8eef4;">
          <tr>
            <td style="padding:28px 28px 8px 28px;font-family:Arial,Helvetica,sans-serif;">
              <div style="border-top:1px solid #e2e8f0;padding-top:14px;margin-bottom:14px;">
                <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                            text-transform:uppercase;color:#94a3b8;text-align:center;">
                  Scheduling Update
            </div>
              </div>
              <h1 style="margin:0 0 14px 0;font-size:26px;line-height:1.25;font-weight:700;
                         color:#231F20;text-align:center;">
                Your Interview Has Been <span style="color:#00ADEE;">Rescheduled</span>
              </h1>
              <p style="margin:0 0 8px 0;font-size:15px;line-height:1.5;color:#475569;text-align:center;">
                Hi {h(candidate_name_raw)},
              </p>
              <p style="margin:0 0 18px 0;font-size:14px;line-height:1.55;color:#64748b;text-align:center;">
                {h(intro)}
              </p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {cta_html}
              </table>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {_detail_row("Company Name", h(company_raw))}
                {_detail_row("Role", h(job_role_raw))}
                {_detail_row("New Date", h(slot["date_display_raw"]))}
                {_detail_row("New Time", h(slot["time_display_raw"]))}
                {_detail_row("Type", h(slot["mode_title"]), slot["mode_sub"])}
                {_detail_row("Duration", h(f'{slot["duration"]} mins'), "Standard Session")}
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 28px 24px 28px;">
              <div style="border-top:1px solid #eef2f7;padding-top:14px;
                          font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#94a3b8;
                          text-align:center;">
                Kovetted · Talent Acquisition
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

            cal = _calendar_url(slot)
            text_body = (
                "SCHEDULING UPDATE\n"
                "Your Interview Has Been Rescheduled\n\n"
                f"Hi {candidate_name_raw},\n\n"
                f"{intro}\n\n"
                f"Company Name: {company_raw}\n"
                f"Role: {job_role_raw}\n"
                f"New Date: {slot['date_display_raw']}\n"
                f"New Time: {slot['time_display_raw']}\n"
                f"Type: {slot['mode_title']} ({slot['mode_sub']})\n"
                f"Duration: {slot['duration']} mins\n"
                + (f"\nAdd to Calendar: {cal}\n" if cal else "")
            )
            return subject, html_body, text_body

        # HR / FYI card — new slot booked from website
        if reschedule_complete and not is_candidate:
            slot = _format_slot_fields()
            candidate_name = h(candidate_name_raw)
            job_role = h(job_role_raw)
            details_url = _details_url()
            details_href = href(details_url)
            cta_html = ""
            if details_href:
                cta_html = f"""
                  <tr>
                    <td align="center" style="padding:22px 0 4px 0;">
                      <a href="{details_href}"
                         style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                         font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;
                         text-decoration:none;padding:12px 22px;border-radius:10px;">
                        View Interview Details
                      </a>
                    </td>
                  </tr>
                """

            subject = f"Interview Successfully Rescheduled - {candidate_name_raw}"
            intro = (
                f"The {interview_level_raw} Interview has been rescheduled successfully. "
                "The candidate has been notified and the updated meeting invitation has been shared."
            )
            if is_fyi:
                subject = f"Interview rescheduled (FYI) - {candidate_name_raw}"
                intro = (
                    f"FYI: The {interview_level_raw} Interview for {candidate_name_raw} "
                    "has been rescheduled successfully."
                )

            html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {_logo_html()}
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:560px;background-color:#ffffff;border-radius:16px;
                      border:1px solid #e8eef4;">
          <tr>
            <td style="padding:28px 28px 8px 28px;font-family:Arial,Helvetica,sans-serif;">
              <div style="border-top:1px solid #e2e8f0;padding-top:14px;margin-bottom:14px;">
                <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                            text-transform:uppercase;color:#94a3b8;text-align:center;">
                  Rescheduling Update
                </div>
              </div>
              <h1 style="margin:0 0 10px 0;font-size:26px;line-height:1.25;font-weight:700;
                         color:#1B75BB;text-align:center;">
                Interview Successfully <span style="color:#00ADEE;">Rescheduled</span>
              </h1>
              <p style="margin:0 0 22px 0;font-size:14px;line-height:1.5;color:#64748b;text-align:center;">
                {h(intro)}
              </p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {_detail_row("Candidate Name", candidate_name)}
                {_detail_row("Role", job_role)}
                {_detail_row("New Date", h(slot["date_display_raw"]))}
                {_detail_row("New Time", h(slot["time_display_raw"]))}
                {_detail_row("Interview Mode", h(slot["mode_title"]), slot["mode_sub"])}
              </table>
              {cta_html}
            </td>
          </tr>
          <tr>
            <td style="padding:18px 28px 24px 28px;">
              <div style="border-top:1px solid #eef2f7;padding-top:14px;
                          font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#94a3b8;
                          text-align:center;">
                Kovetted · Talent Acquisition
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

            text_body = (
                "RESCHEDULING UPDATE\n"
                "Interview Successfully Rescheduled\n\n"
                f"{intro}\n\n"
                f"Candidate Name: {candidate_name_raw}\n"
                f"Role: {job_role_raw}\n"
                f"New Date: {slot['date_display_raw']}\n"
                f"New Time: {slot['time_display_raw']}\n"
                f"Interview Mode: {slot['mode_title']}"
                + (f" ({slot['mode_sub']})" if slot["mode_sub"] else "")
                + "\n"
                + (f"\nView details: {details_url}\n" if details_url else "")
            )
            return subject, html_body, text_body

        # Candidate / HR booking-window reschedule (new slot picker link)
        candidate_name_raw = (data.get("candidate_name") or "Candidate").strip() or "Candidate"
        job_role_raw = (data.get("job_role") or "N/A").strip() or "N/A"
        interview_level_raw = (
            (data.get("interview_level") or "Level 2").strip() or "Level 2"
        )
        company_raw = (data.get("company_name") or "Company").strip() or "Company"
        candidate_name = h(candidate_name_raw)
        job_role = h(job_role_raw)
        booking_url_raw = (data.get("booking_url") or "").strip()
        booking_href = href(booking_url_raw)
        date_range_start = (data.get("date_range_start") or "").strip()
        date_range_end = (data.get("date_range_end") or "").strip()
        previous_date = (data.get("previous_scheduled_date") or "").strip()
        previous_time = (data.get("previous_scheduled_time") or "").strip()

        range_display = ""
        if date_range_start and date_range_end:
            range_display = f"{h(date_range_start)} – {h(date_range_end)}"

        previous_display = ""
        if previous_date or previous_time:
            parts = []
            if previous_date:
                parts.append(h(previous_date))
            if previous_time:
                parts.append(h(previous_time))
            previous_display = " · ".join(parts)

        logo_block = _logo_html()
        details_url = _details_url()
        details_href = href(details_url)

        if is_candidate:
            subject = f"Interview Rescheduled - {job_role_raw}"
            cta_html = ""
            if booking_href:
                cta_html = f"""
                  <tr>
                    <td align="left" style="padding:8px 0 22px 0;">
                      <a href="{booking_href}"
                         style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                         font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;
                         text-decoration:none;padding:12px 20px;border-radius:10px;">
                        Choose New Time
                      </a>
                    </td>
                  </tr>
            """
            else:
                cta_html = """
                  <tr>
                    <td style="padding:8px 0 22px 0;font-family:Arial,Helvetica,sans-serif;
                               font-size:14px;color:#b91c1c;">
                      Scheduling link unavailable — please contact the recruiter.
                    </td>
                  </tr>
                """

            previous_row = ""
            if previous_display:
                previous_row = _detail_row(
                    "Previous Slot (cancelled)",
                    previous_display,
                )
            range_row = ""
            if range_display:
                range_row = _detail_row("Available Dates", range_display)

            html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {logo_block}
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:560px;background-color:#ffffff;border-radius:16px;
                      border:1px solid #e8eef4;border-top:4px solid #00ADEE;">
          <tr>
            <td style="padding:28px 28px 8px 28px;font-family:Arial,Helvetica,sans-serif;">
              <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                          text-transform:uppercase;color:#00ADEE;margin:0 0 10px 0;">
                Scheduling Update
              </div>
              <h1 style="margin:0 0 14px 0;font-size:26px;line-height:1.25;font-weight:700;color:#231F20;">
                Your interview needs to be <span style="color:#00ADEE;">rescheduled</span>
              </h1>
              <p style="margin:0 0 20px 0;font-size:14px;line-height:1.55;color:#64748b;">
                Hi {candidate_name}, your {h(interview_level_raw)} interview for {job_role} at
                {h(company_raw)} needs a new time. Pick an open slot using the button below.
              </p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {previous_row}
                {range_row}
                {_detail_row("Role", job_role)}
              </table>
              {cta_html}
              <p style="margin:0;font-size:13px;line-height:1.5;color:#94a3b8;">
                Available times refresh from interviewer calendars — only open slots are shown.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

            text_previous = ""
            if previous_date or previous_time:
                text_previous = (
                    f"Previous slot (cancelled): {previous_date} {previous_time}".strip()
                )
            text_range = (
                f"Available dates: {date_range_start} to {date_range_end}."
                if date_range_start and date_range_end
                else ""
            )
            text_body = (
                "SCHEDULING UPDATE\n"
                "Your interview needs to be rescheduled\n\n"
                f"Hi {candidate_name_raw},\n\n"
                f"Your {interview_level_raw} interview for {job_role_raw} needs a new time.\n"
                f"{text_previous}\n"
                f"{text_range}\n"
                f"Choose a new time: {booking_url_raw or 'Contact recruiter'}\n"
            )
            return subject, html_body, text_body

        l2_html, l2_text = self._l2_pdf_note(data)

        if is_fyi:
            subject = f"Interview reschedule (FYI) - {candidate_name_raw}"
            intro = (
                f"FYI: The {interview_level_raw} interview for {candidate_name_raw} "
                f"({job_role_raw}) is being rescheduled."
            )
            previous_row = ""
            if previous_display:
                previous_row = _detail_row("Previous Slot", previous_display)

            html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8" /><title>{h(subject)}</title></head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td align="center" style="padding:36px 16px;">
      {logo_block}
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
             style="width:100%;max-width:560px;background:#fff;border-radius:16px;border:1px solid #e8eef4;">
        <tr><td style="padding:28px;font-family:Arial,Helvetica,sans-serif;">
          <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
                      color:#94a3b8;text-align:center;margin-bottom:12px;">Rescheduling Update (FYI)</div>
          <h1 style="margin:0 0 12px 0;font-size:24px;text-align:center;color:#231F20;">Interview Being Rescheduled</h1>
          <p style="margin:0 0 20px 0;font-size:14px;color:#64748b;text-align:center;">{h(intro)}</p>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            {_detail_row("Candidate", candidate_name)}
            {_detail_row("Role", job_role)}
            {previous_row}
          </table>
          <p style="margin:20px 0 0 0;font-size:13px;color:#94a3b8;text-align:center;">
            No action required. You will receive another update once a new time is confirmed.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
            text_body = f"{intro}\nNo action required.\n"
            return subject, html_body, text_body

        subject = f"Interview Reschedule Link Sent - {candidate_name_raw}"
        intro = (
            f"A new self-scheduling link has been sent to {candidate_name_raw} for "
            f"{job_role_raw}."
        )
        previous_row = ""
        if previous_display:
            previous_row = _detail_row("Previous Slot", previous_display)
        range_row = ""
        if range_display:
            range_row = _detail_row("New Date Range", range_display)

        hr_cta = ""
        if details_href:
            hr_cta = f"""
              <tr>
                <td align="center" style="padding:16px 0 4px 0;">
                  <a href="{details_href}"
                     style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                     font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;
                     text-decoration:none;padding:12px 22px;border-radius:10px;">
                    View Interview Details
                  </a>
                </td>
              </tr>
            """

        l2_note = l2_html if data.get("l2_pdf_attached") else ""

        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8" /><title>{h(subject)}</title></head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td align="center" style="padding:36px 16px;">
      {logo_block}
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
             style="width:100%;max-width:560px;background:#fff;border-radius:16px;border:1px solid #e8eef4;">
        <tr><td style="padding:28px;font-family:Arial,Helvetica,sans-serif;">
          <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
                      color:#94a3b8;text-align:center;margin-bottom:12px;">Rescheduling Update</div>
          <h1 style="margin:0 0 12px 0;font-size:24px;text-align:center;color:#1B75BB;">
            Reschedule Link <span style="color:#00ADEE;">Sent</span>
          </h1>
          <p style="margin:0 0 20px 0;font-size:14px;color:#64748b;text-align:center;">{h(intro)}</p>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            {_detail_row("Candidate", candidate_name)}
            {_detail_row("Role", job_role)}
            {previous_row}
            {range_row}
          </table>
          {l2_note}
          {hr_cta}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

        text_body = (
            f"{intro}\n\n"
            f"Candidate: {candidate_name_raw}\n"
            f"Role: {job_role_raw}\n"
            + (f"Previous slot: {previous_date} {previous_time}\n" if previous_date or previous_time else "")
            + (f"Date range: {date_range_start} to {date_range_end}\n" if date_range_start and date_range_end else "")
            + f"{l2_text}\n"
        )
        return subject, html_body, text_body

    def _interview_confirmed_template(self, data: dict) -> Tuple[str, str, str]:
        """Card-only email when an interview slot is confirmed."""
        h, href = self._h, self._href
        candidate_name_raw = (data.get("candidate_name") or "Candidate").strip() or "Candidate"
        job_role_raw = (data.get("job_role") or "N/A").strip() or "N/A"
        interview_level_raw = (
            (data.get("interview_level") or "Level 2").strip() or "Level 2"
        )
        interviewer_raw = (
            (data.get("interviewer_name") or "HR Team").strip() or "HR Team"
        )
        candidate_name = h(candidate_name_raw)
        job_role = h(job_role_raw)
        interviewer_name = h(interviewer_raw)
        timezone_raw = (data.get("timezone") or "").strip()
        is_candidate = bool(data.get("is_candidate"))
        is_fyi = bool(data.get("is_fyi"))
        in_person = str(data.get("interview_mode") or "").lower() in {
            "in_person",
            "in-person",
            "onsite",
        }

        date_raw = (data.get("scheduled_date") or "").strip()
        date_display_long = date_raw or "—"
        date_display_short = date_raw or "—"
        try:
            parsed_date = datetime.strptime(date_raw, "%Y-%m-%d")
            date_display_long = parsed_date.strftime("%A, %b %d")
            date_display_short = parsed_date.strftime("%d/%m/%Y")
        except Exception:
            pass

        start_raw = (data.get("scheduled_time") or "").strip()
        duration = int(data.get("duration_minutes") or 60)
        time_display_long = start_raw or "—"
        time_display_short = start_raw or "—"
        try:
            start_dt = datetime.strptime(start_raw, "%I:%M %p")
            end_dt = start_dt + timedelta(minutes=duration)
            start_label = start_dt.strftime("%I:%M %p").lstrip("0")
            end_label = end_dt.strftime("%I:%M %p").lstrip("0")
            time_display_long = f"{start_label} - {end_label}"
            # Compact: 10AM-12PM
            start_compact = start_dt.strftime("%I%p").lstrip("0").replace(":00", "")
            end_compact = end_dt.strftime("%I%p").lstrip("0").replace(":00", "")
            time_display_short = f"{start_compact}-{end_compact}"
            if timezone_raw:
                time_display_long = f"{time_display_long} ({timezone_raw})"
        except Exception:
            if timezone_raw and start_raw:
                time_display_long = f"{start_raw} ({timezone_raw})"

        if in_person:
            mode_title = "In-person"
            mode_sub = (data.get("location_address") or "").strip() or "Office"
        else:
            mode_title = "Video Call"
            mode_sub = self._video_mode_sub(data)

        details_url = ""
        try:
            base = self._frontend_base_url()
            if base:
                job_id = str(data.get("job_id") or "").strip()
                details_url = (
                    f"{base}/recruiters/pipeline/{job_id}/applicants"
                    if job_id
                    else f"{base}/recruiters/dashboard"
                )
        except Exception:
            pass

        logo_html = self._kovetted_logo_html()

        calendar_url = self._add_to_calendar_url(
            data,
            duration_minutes=duration,
            timezone_name=timezone_raw,
            title=f"Interview: {job_role_raw}",
            details=(data.get("meeting_link") or data.get("location_address") or ""),
            location=(data.get("meeting_link") or data.get("location_address") or "").strip(),
        )
        if not calendar_url:
            calendar_url = (data.get("meeting_link") or data.get("maps_url") or "").strip()

        # --- Candidate confirmation card (simple success notice) ---
        if is_candidate:
            subject = f"Interview Scheduled - {job_role_raw}"
            calendar_href = href(calendar_url)
            cta_html = ""
            if calendar_href:
                cta_html = f"""
                  <a href="{calendar_href}"
                     style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                     font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;
                     letter-spacing:0.04em;text-decoration:none;padding:14px 28px;
                     border-radius:999px;box-shadow:0 8px 18px rgba(0,173,238,0.28);">
                    ADD TO CALENDAR &rarr;
                  </a>
                """

            html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {logo_html}
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:480px;background-color:#ffffff;border-radius:18px;
                      border:1px solid #e8eef4;">
          <tr>
            <td align="center" style="padding:36px 28px 32px 28px;font-family:Arial,Helvetica,sans-serif;">
              <h1 style="margin:0 0 12px 0;font-size:28px;line-height:1.25;font-weight:700;color:#231F20;">
                Interview <span style="color:#00ADEE;">Scheduled</span>
              </h1>
              <p style="margin:0 0 26px 0;font-size:14px;line-height:1.55;color:#64748b;max-width:340px;">
                Your interview has been successfully scheduled. We look forward to meeting you.
              </p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                     style="max-width:360px;background-color:#ffffff;border:1px solid #e8eef4;
                            border-radius:14px;margin:0 auto 24px auto;">
                <tr>
                  <td style="padding:16px 18px;border-bottom:1px solid #eef2f7;">
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                      <tr>
                        <td width="28" valign="middle" style="font-size:16px;color:#00ADEE;">&#128197;</td>
                        <td style="font-family:Arial,Helvetica,sans-serif;">
                          <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;
                                      text-transform:uppercase;color:#94a3b8;">Date</div>
                          <div style="font-size:15px;font-weight:700;color:#231F20;margin-top:2px;">
                            {h(date_display_short)}
            </div>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
                <tr>
                  <td style="padding:16px 18px;">
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                      <tr>
                        <td width="28" valign="middle" style="font-size:16px;color:#00ADEE;">&#128337;</td>
                        <td style="font-family:Arial,Helvetica,sans-serif;">
                          <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;
                                      text-transform:uppercase;color:#94a3b8;">Time</div>
                          <div style="font-size:15px;font-weight:700;color:#231F20;margin-top:2px;">
                            {h(time_display_short)}
                          </div>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
              {cta_html}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

            text_body = (
                "Interview Scheduled\n\n"
                "Your interview has been successfully scheduled. We look forward to meeting you.\n\n"
                f"Date: {date_display_short}\n"
                f"Time: {time_display_short}\n"
                + (f"\nAdd to Calendar: {calendar_url}\n" if calendar_url else "")
            )
            return subject, html_body, text_body

        # --- HR / FYI detailed card ---
        date_display = h(date_display_long)
        time_display = h(time_display_long)
        mode_title_h = h(mode_title)
        details_href = href(details_url)
        cta_html = ""
        if details_href:
            cta_html = f"""
              <tr>
                <td align="center" style="padding:22px 0 4px 0;">
                  <a href="{details_href}"
                     style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                     font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;
                     text-decoration:none;padding:12px 22px;border-radius:10px;">
                    View Interview Details
                  </a>
                </td>
              </tr>
            """

        if is_fyi:
            subject = f"Interview scheduled (FYI) - {candidate_name_raw}"
            intro = (
                f"{candidate_name_raw} has confirmed attendance for the "
                f"{interview_level_raw} Interview (FYI)."
            )
        else:
            subject = f"Interview Successfully Scheduled - {candidate_name_raw}"
            intro = (
                f"{candidate_name_raw} has selected a time slot and confirmed attendance "
                f"for the {interview_level_raw} Interview"
            )

        def detail_row(label: str, value: str, sub: str = "") -> str:
            sub_html = (
                f'<div style="font-size:12px;color:#64748b;margin-top:2px;">{h(sub)}</div>'
                if sub
                else ""
            )
            return f"""
              <tr>
                <td style="padding:0 0 10px 0;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                         style="background-color:#EAF6FC;border-radius:10px;">
                    <tr>
                      <td style="padding:14px 16px;font-family:Arial,Helvetica,sans-serif;">
                        <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;
                                    text-transform:uppercase;color:#64748b;margin:0 0 4px 0;">
                          {h(label)}
                        </div>
                        <div style="font-size:15px;font-weight:700;color:#231F20;line-height:1.35;">
                          {value}
                        </div>
                        {sub_html}
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            """

        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {logo_html}
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:560px;background-color:#ffffff;border-radius:16px;
                      border:1px solid #e8eef4;">
          <tr>
            <td style="padding:28px 28px 8px 28px;font-family:Arial,Helvetica,sans-serif;">
              <div style="border-top:1px solid #e2e8f0;padding-top:14px;margin-bottom:14px;">
                <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                            text-transform:uppercase;color:#94a3b8;text-align:center;">
                  Interview Confirmed
                </div>
              </div>
              <h1 style="margin:0 0 10px 0;font-size:26px;line-height:1.25;font-weight:700;
                         color:#1B75BB;text-align:center;">
                Interview Successfully <span style="color:#00ADEE;">Scheduled</span>
              </h1>
              <p style="margin:0 0 22px 0;font-size:14px;line-height:1.5;color:#64748b;text-align:center;">
                {h(intro)}
              </p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {detail_row("Candidate Name", candidate_name)}
                {detail_row("Role", job_role)}
                {detail_row("Date", date_display)}
                {detail_row("Time", time_display)}
                {detail_row("Interviewer Name", interviewer_name)}
                {detail_row("Interview Mode", mode_title_h, mode_sub)}
              </table>
              {cta_html}
            </td>
          </tr>
          <tr>
            <td style="padding:18px 28px 24px 28px;">
              <div style="border-top:1px solid #eef2f7;padding-top:14px;
                          font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#94a3b8;
                          text-align:center;">
                Kovetted · Talent Acquisition
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

        text_body = (
            "INTERVIEW CONFIRMED\n"
            "Interview Successfully Scheduled\n\n"
            f"{intro}\n\n"
            f"Candidate Name: {candidate_name_raw}\n"
            f"Role: {job_role_raw}\n"
            f"Date: {date_display_long or '—'}\n"
            f"Time: {time_display_long}\n"
            f"Interviewer Name: {interviewer_raw}\n"
            f"Interview Mode: {mode_title}"
            + (f" ({mode_sub})" if mode_sub else "")
            + "\n"
            + (f"\nView details: {details_url}\n" if details_url else "")
        )

        return subject, html_body, text_body

    def _interview_cancelled_template(self, data: dict) -> Tuple[str, str, str]:
        candidate_name = data.get("candidate_name", "Candidate")
        job_role = data.get("job_role", "N/A")
        scheduled_date = data.get("scheduled_date", "")
        scheduled_time = data.get("scheduled_time", "")
        is_candidate = data.get("is_candidate", False)
        when = f"{scheduled_date} {scheduled_time}".strip()
        if is_candidate:
            subject = f"Interview cancelled - {job_role}"
            content = f"""
            <p>Hi {candidate_name},</p>
            <p>Your interview for <strong>{job_role}</strong>{f' scheduled for <strong>{when}</strong>' if when else ''} has been cancelled.</p>
            <p>The hiring team will contact you if a new time is arranged.</p>
            """
            text_body = f"Hi {candidate_name},\n\nYour interview for {job_role} has been cancelled.\n"
        else:
            subject = f"Interview cancelled - {candidate_name}"
            content = f"""
            <p>Hi Team,</p>
            <p>The interview for <strong>{candidate_name}</strong> ({job_role}) has been cancelled.</p>
            """
            text_body = f"Interview cancelled for {candidate_name} ({job_role}).\n"
        html_body = self._get_base_template(subject, content, job_role)
        return subject, html_body, text_body

    def _candidate_apply_template(self, data: dict) -> Tuple[str, str, str]:
        """HR card email: new candidate applied — single 3-column summary card."""
        candidate_name = self._h(
            (data.get("candidate_name") or "").strip() or "Candidate"
        )
        job_role = self._h((data.get("job_role") or "").strip() or "N/A")
        raw_date = (data.get("submission_date") or "").strip()
        if raw_date:
            submission_date = self._h(raw_date)
        else:
            submission_date = self._h(datetime.now().strftime("%b %d, %Y"))

        details_url = ""
        try:
            base = self._frontend_base_url()
            if base:
                job_id = str(data.get("job_id") or "").strip()
                if job_id:
                    details_url = f"{base}/recruiters/pipeline/{job_id}/applicants"
                else:
                    details_url = f"{base}/recruiters/dashboard"
        except Exception:
            details_url = ""

        logo_html = self._kovetted_logo_html(
            width=160, height=36, margin_bottom=28, max_width=180
        )

        subject = "A New Candidate Has Applied"

        details_href = self._href(details_url)
        cta_html = ""
        if details_href:
            cta_html = f"""
              <tr>
                <td align="center" style="padding:24px 8px 0 8px;">
                  <a href="{details_href}"
                     style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                     font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;
                     text-decoration:none;padding:12px 22px;border-radius:10px;">
                    View Applicants
                  </a>
                </td>
              </tr>
            """

        def detail_cell(label: str, value: str, *, uppercase_value: bool = False) -> str:
            value_transform = "text-transform:uppercase;" if uppercase_value else ""
            return f"""
              <td width="33.33%" valign="top" style="width:33.33%;padding:22px 18px;font-family:Arial,Helvetica,sans-serif;">
                <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;color:#94a3b8;
                            text-transform:uppercase;margin:0 0 8px 0;line-height:1.3;">{label}</div>
                <div style="font-size:15px;font-weight:700;color:#231F20;line-height:1.35;{value_transform}">{value}</div>
              </td>
            """

        # Standalone card — matches Figma: logo, title, subtitle, one 3-col info card.
        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{subject}</title>
  <!--[if mso]>
  <style type="text/css">
    body, table, td {{ font-family: Arial, Helvetica, sans-serif !important; }}
  </style>
  <![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#ffffff;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#ffffff;">
    <tr>
      <td align="center" style="padding:40px 16px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="width:100%;max-width:600px;background-color:#ffffff;">
          <tr>
            <td align="center" style="padding:0 8px 8px 8px;">
              {logo_html}
              <h1 style="margin:0 0 10px 0;font-family:Arial,Helvetica,sans-serif;font-size:28px;line-height:1.25;font-weight:700;color:#231F20;text-align:center;">
                A New Candidate Has Applied
              </h1>
              <p style="margin:0 0 28px 0;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#64748b;text-align:center;">
                A new application has been received for your job posting.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 8px;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
                     style="width:100%;background-color:#E8F6FC;border-radius:12px;">
                <tr>
                  {detail_cell("Applicant Name", candidate_name, uppercase_value=True)}
                  {detail_cell("Role", job_role)}
                  {detail_cell("Submission Date", submission_date)}
                </tr>
              </table>
            </td>
          </tr>
          {cta_html}
          <tr>
            <td align="center" style="padding:28px 8px 0 8px;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="border-top:1px solid #e2e8f0;font-size:0;line-height:0;">&nbsp;</td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

        text_body = (
            "A New Candidate Has Applied\n"
            "A new application has been received for your job posting.\n\n"
            f"Applicant Name: {data.get('candidate_name') or 'Candidate'}\n"
            f"Role: {data.get('job_role') or 'N/A'}\n"
            f"Submission Date: {raw_date or datetime.now().strftime('%b %d, %Y')}\n"
            + (f"\nView applicants: {details_url}\n" if details_url else "")
        )

        return subject, html_body, text_body

    def _candidate_rejected_template(self, data: dict) -> Tuple[str, str, str]:
        """Card-only rejection email for L0 / L1 outcomes."""
        h, href = self._h, self._href
        job_role_raw = (data.get("job_role") or "this role").strip() or "this role"
        job_role = h(job_role_raw)
        date_sent = datetime.now().strftime("%b %d, %Y")

        openings_url = ""
        try:
            base = self._frontend_base_url()
            if base:
                openings_url = f"{base}/search"
        except Exception:
            pass

        logo_html = self._kovetted_logo_html()

        openings_href = href(openings_url)
        cta_html = ""
        if openings_href:
            cta_html = f"""
              <a href="{openings_href}"
                 style="display:inline-block;background-color:#00ADEE;color:#ffffff;
                 font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;
                 text-decoration:none;padding:12px 22px;border-radius:10px;">
                View Other Openings &rarr;
              </a>
            """

        subject = f"Application Update - {job_role_raw}"
        body_copy = (
            f"Thank you for your interest in the {job_role_raw} position. "
            "At this time, we have decided to move forward with other candidates. "
            "We appreciate your time and wish you the best in your search."
        )

        # Keep job title visually emphasized in HTML without exposing raw markup.
        body_html = (
            f"Thank you for your interest in the <strong style=\"color:#231F20;\">{job_role}</strong> position. "
            "At this time, we have decided to move forward with other candidates. "
            "We appreciate your time and wish you the best in your search."
        )

        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{h(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f6f9;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        {logo_html}
        <table role="presentation" width="520" cellpadding="0" cellspacing="0" border="0"
               style="width:100%;max-width:520px;background-color:#ffffff;border-radius:16px;
                      border:1px solid #e8eef4;border-top:4px solid #00ADEE;">
          <tr>
            <td align="center" style="padding:36px 32px 28px 32px;font-family:Arial,Helvetica,sans-serif;">
              <div style="width:56px;height:56px;border-radius:999px;background-color:#EAF6FC;
                          margin:0 auto 18px auto;line-height:56px;text-align:center;">
                <span style="font-size:22px;color:#1B75BB;">&#9993;</span>
              </div>
              <h1 style="margin:0 0 14px 0;font-size:24px;line-height:1.3;font-weight:700;color:#231F20;">
                Application Update
              </h1>
              <p style="margin:0 0 24px 0;font-size:15px;line-height:1.6;color:#64748b;max-width:420px;">
                {body_html}
              </p>
              <div style="border-top:1px solid #eef2f7;padding-top:22px;margin-bottom:22px;">
                {cta_html}
              </div>
              <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                          text-transform:uppercase;color:#94a3b8;margin:0 0 4px 0;">
                Date Sent
              </div>
              <div style="font-size:13px;color:#64748b;">
                {h(date_sent)}
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

        text_body = (
            "Application Update\n\n"
            f"{body_copy}\n\n"
            + (f"View Other Openings: {openings_url}\n\n" if openings_url else "")
            + f"Date Sent: {date_sent}\n"
        )
        return subject, html_body, text_body

    def _l1_passed_template(self, data: dict) -> Tuple[str, str, str]:
        candidate_name = data.get("candidate_name", "Candidate")
        job_role = data.get("job_role", "this role")
        subject = f"L1 passed - {candidate_name}"
        content = f"""
        <p>Hi Team,</p>
        <p><strong>{candidate_name}</strong> has passed the L1 interview for <strong>{job_role}</strong>.</p>
        <p>Please review the score in the dashboard and schedule the next interview if you want to proceed.</p>
        <p>Regards,<br>
        Recruit Assist</p>
        """
        html_body = self._get_base_template(subject, content, job_role)
        text_body = f"""
Hi Team,

{candidate_name} has passed the L1 interview for {job_role}.

Please review the score in the dashboard and schedule the next interview if you want to proceed.

Regards,
Recruit Assist
        """
        return subject, html_body, text_body

    def _default_template(self, data: dict) -> Tuple[str, str, str]:
        """Default template for unknown action types"""
        action = data.get('action', 'Unknown Action')
        job_role = data.get('job_role', 'N/A')
        
        subject = f"HR Dashboard Notification - {action}"
        
        content = f"""
        <h3>System Notification</h3>
        <div class="info-box">
            <p><span class="label">Action:</span> {action}</p>
            <p><span class="label">Details:</span> {str(data)}</p>
        </div>
        """
        
        html_body = self._get_base_template("System Notification", content, job_role)
        
        text_body = f"""
System Notification

Action: {action}
Details: {str(data)}
        """
        
        return subject, html_body, text_body

