import os
import smtplib
from typing import List, Optional, Dict, Any
from email.message import EmailMessage
from utils.constants import PLATFORM_CONFIG
import html as _html
import logging

logger = logging.getLogger(__name__)


def _get_bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _dedupe_recipients(recipients: Optional[List[str]]) -> List[str]:
    if not recipients:
        return []
    seen = set()
    unique: List[str] = []
    for r in recipients:
        if isinstance(r, str):
            t = r.strip()
            if t and t not in seen:
                seen.add(t)
                unique.append(t)
    return unique


def _format_summary_subject(summary: Dict[str, Any]) -> str:
    status_raw = (summary.get("status") or "unknown").strip().lower()
    status_emoji = "✅" if status_raw == "success" else ("❌" if status_raw == "failure" else "ℹ️")
    status_title = "Success" if status_raw == "success" else ("Failure" if status_raw == "failure" else "Info")
    jira_id = summary.get("jira_id", "N/A")
    domain = summary.get("selected_domain", "N/A")
    run_id = summary.get("run_id", "")
    run_id_suffix = f" [{run_id}]" if run_id else ""
    return f"{status_emoji} TestGenie: {jira_id} [{domain}]{run_id_suffix} — {status_title}"


def _format_summary_body(summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    add = lines.append

    # Header
    status_raw = (summary.get("status") or "unknown").strip().lower()
    status_emoji = "✅" if status_raw == "success" else ("❌" if status_raw == "failure" else "ℹ️")
    add(f"{status_emoji} TestGenie Run Summary")
    add("")

    # Submitted block (only for initiation notifications)
    if status_raw == "initiated":
        submitted_eastern = summary.get("submitted_at_eastern")
        submitted_utc = summary.get("submitted_at_utc")
        initial_message = summary.get("message")
        if submitted_eastern or submitted_utc or initial_message:
            add("🕒 Submitted")
            if submitted_eastern:
                add(f"- 🕒 Eastern: {submitted_eastern}")
            if submitted_utc:
                add(f"- 🌐 UTC: {submitted_utc}")
            if initial_message:
                add(f"- 💬 Message: {initial_message}")
            add("")

    # Inputs
    add("🧾 Inputs")
    add(f"- 🏷 Jira ID: {summary.get('jira_id', 'N/A')}")
    if summary.get("run_id"):
        add(f"- 🆔 Run ID: {summary.get('run_id')}")
    add(f"- 🗂 Domain: {summary.get('selected_domain', 'N/A')}")
    if summary.get("push_to_github") is not None:
        add(f"- 🐙 Push to GitHub: {'✅ Yes' if summary.get('push_to_github') else '❌ No'}")
    if summary.get("record_video") is not None:
        add(f"- 🎥 Record video: {'✅ Yes' if summary.get('record_video') else '❌ No'}")
    if summary.get("generate_step_definitions") is not None:
        add(f"- 🧩 Generate step definitions: {'✅ Yes' if summary.get('generate_step_definitions') else '❌ No'}")
    add("")

    # Results
    add("🎯 Results")
    add(f"- {status_emoji} Status: {summary.get('status', 'unknown')}")
    # Show version only when it represents GitHub branch auto-version
    if summary.get("push_to_github") and summary.get("version"):
        add(f"- 🏷 Branch version: {summary.get('version')}")
    total = summary.get("scenarios_total")
    passed = summary.get("scenarios_passed")
    failed = summary.get("scenarios_failed")
    if total is not None and passed is not None and failed is not None:
        add(f"- 📊 Scenarios: ✅ {passed} / {total}  |  ❌ {failed} failed")
    if summary.get("execution_seconds") is not None:
        add(f"- ⏱ Execution time (s): {int(summary.get('execution_seconds', 0))}")
    add("")

    # GitHub/PR
    if summary.get("github_repo") or summary.get("branch_name") or summary.get("pr_url") or summary.get("pr_number"):
        add("🐙 GitHub")
        if summary.get("github_repo"):
            add(f"- 📦 Repository: {summary.get('github_repo')}")
        if summary.get("branch_name"):
            add(f"- 🌿 Branch: {summary.get('branch_name')}")
        if summary.get("pr_url"):
            add(f"- 🔗 Pull Request: {summary.get('pr_url')}")
        if summary.get("pr_number") is not None:
            add(f"- # PR Number: {summary.get('pr_number')}")
        add("")

    # Jenkins
    if summary.get("jenkins_project") or summary.get("jenkins_queue") or summary.get("jenkins_job_url"):
        add("🧰 Jenkins")
        if summary.get("jenkins_project"):
            add(f"- 🔧 Job: {summary.get('jenkins_project')}")
        if summary.get("jenkins_job_url"):
            add(f"- 🔗 Job URL: {summary.get('jenkins_job_url')}")
        if summary.get("jenkins_queue"):
            add(f"- ⏳ Queue URL: {summary.get('jenkins_queue')}")
        add("")

    # Generated package (prefer ZIP display over raw .ts name)
    if summary.get("download_zip_filename") or summary.get("download_zip_url") or summary.get("generated_ts_filename") or summary.get("jira_id"):
        add("📦 Generated Package")
        zip_name = summary.get("download_zip_filename")
        if not zip_name:
            # fallback to conventional name if only .ts name/jira provided
            jira = summary.get("jira_id")
            if jira:
                zip_name = f"{jira}_playwright_scripts.zip"
        if zip_name:
            add(f"- 🗜 Scripts ZIP: {zip_name}")
        if summary.get("download_zip_url"):
            add(f"- 🔽 Download: {summary.get('download_zip_url')}")
        add("")

    # Recordings (links if base URL provided)
    recs = summary.get("recordings") or []
    if recs:
        add("🎥 Recordings")
        base = summary.get("recordings_download_base_url")
        from urllib.parse import urlparse
        for r in recs:
            scen = r.get("scenario_number", "?")
            zip_filename = r.get("zip_filename")
            if not zip_filename:
                zip_url = r.get("zip_url") or ""
                try:
                    parsed = urlparse(zip_url)
                    # handle s3://, http(s):// and plain paths
                    zip_filename = (parsed.path.rsplit("/", 1)[-1]) if parsed.scheme else (zip_url.rsplit("/", 1)[-1])
                except Exception:
                    zip_filename = zip_url or "recording.zip"
            if base and zip_filename:
                add(f"- ▶️ Scenario {scen}: {zip_filename}  ({base}/{zip_filename})")
            else:
                add(f"- ▶️ Scenario {scen}: {zip_filename}")
        add("")

    # Errors (for failure mode)
    if summary.get("error_message"):
        # Sanitize to remove recording tags/links
        import re as _re
        err = str(summary.get("error_message"))
        err = _re.sub(r"RECORDING_ZIP:\s*\S+", "", err).strip()
        add("❌ Error")
        add(f"- 🧾 Message: {err}")
    if summary.get("error_stage"):
        add(f"- 🧩 Stage: {summary.get('error_stage')}")

    # Scenario-level friendly reasons (if any)
    reasons = summary.get("scenario_failure_reasons") or []
    details = summary.get("scenario_failure_details") or []
    if reasons:
        add("")
        add("⚠️ Issues detected")
        # Map details by scenario for technical lines
        scen_to_detail = {}
        for d in details:
            try:
                scen_to_detail[str(d.get("scenario_number"))] = d.get("technical")
            except Exception:
                pass
        for r in reasons:
            scen = r.get("scenario_number", "?")
            reason = r.get("reason", "Scenario failed")
            add(f"- Scenario {scen}: {reason}")
            tech = scen_to_detail.get(str(scen)) or ""
            if tech:
                # Keep technical line compact
                if len(tech) > 240:
                    tech = tech[:240] + "…"
                add(f"  · Technical: {tech}")

    # Basic guidance for common failures
    if summary.get("status") == "failure":
        add("")
        add("💡 Tips")
        add("- Check the first tag format in the feature file (e.g., payments-ABC123).")
        add("- Verify GitHub token and repo permissions if PR/branch failed.")
        add("- Ensure Jenkins credentials and job name are correct if triggering failed.")
        add("- Review the scenario steps and any API request JSON for syntax issues.")

    return "\n".join(lines)


def _format_summary_html(summary: Dict[str, Any]) -> str:
    # Minimal, inline-styled HTML suitable for most clients
    esc = _html.escape
    status = (summary.get("status") or "unknown").strip().lower()
    is_success = status == "success"
    is_failure = status == "failure"
    banner_bg = "#16a34a" if is_success else ("#dc2626" if is_failure else "#334155")
    emoji = "✅" if is_success else ("❌" if is_failure else "ℹ️")

    def yes_no(val: Optional[bool]) -> str:
        if val is None:
            return "—"
        return "✅ Yes" if val else "❌ No"

    def row(label: str, value: Optional[str]) -> str:
        v = esc(str(value)) if value is not None else "—"
        return f"<tr><td style='padding:4px 8px;color:#334155'>{esc(label)}</td><td style='padding:4px 8px;color:#111827'><strong>{v}</strong></td></tr>"

    def link_row(label: str, url: Optional[str]) -> str:
        if url:
            u = esc(url)
            return f"<tr><td style='padding:4px 8px;color:#334155'>{esc(label)}</td><td style='padding:4px 8px'><a href='{u}' style='color:#2563eb;text-decoration:none'>{u}</a></td></tr>"
        return row(label, None)

    def cta(label: str, url: Optional[str], bg: str = "#2563eb") -> str:
        if not url:
            return ""
        u = esc(url)
        return (
            f"<a href='{u}' "
            f"style='display:inline-block;margin:4px 8px 0 0;padding:8px 12px;background:{bg};color:#ffffff;text-decoration:none;border-radius:6px;'>"
            f"{esc(label)}</a>"
        )

    # Compute some derived values
    push_to_github = summary.get("push_to_github")
    record_video = summary.get("record_video")
    gen_steps = summary.get("generate_step_definitions")
    pr_url = summary.get("pr_url")
    jenkins_job_url = summary.get("jenkins_job_url")
    download_zip_filename = summary.get("download_zip_filename")
    download_zip_url = summary.get("download_zip_url")
    recordings = summary.get("recordings") or []
    recordings_base = summary.get("recordings_download_base_url")

    # Header/banner
    html = [
        "<!doctype html>",
        "<html><body style='margin:0;padding:0;background:#f8fafc;font-family:Segoe UI, Helvetica, Arial, sans-serif;'>",
        f"<div style='background:{banner_bg};color:#ffffff;padding:14px 16px;font-size:16px;font-weight:600'>{emoji} "
        f"TestGenie: {esc(str(summary.get('jira_id','N/A')))} [{esc(str(summary.get('selected_domain','N/A')))}] — "
        f"{'Success' if is_success else ('Failure' if is_failure else 'Info')}</div>",
        "<div style='padding:16px'>",
        "<div style='background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;padding:14px 16px;'>",
    ]

    # Inputs
    html += [
        "<h3 style='margin:0 0 8px 0;color:#111827'>🧾 Inputs</h3>",
        "<table style='border-collapse:collapse;width:100%;margin-bottom:12px'>",
        row("Jira ID", summary.get("jira_id")),
        (row("Run ID", summary.get("run_id")) if summary.get("run_id") else ""),
        row("Domain", summary.get("selected_domain")),
        row("Push to GitHub", yes_no(push_to_github)),
        row("Record video", yes_no(record_video)),
        row("Generate step definitions", yes_no(gen_steps)),
        "</table>",
    ]
    # Add submitted/message rows only for initiation
    if status == "initiated":
        html.insert(-2, row("Message", summary.get("message")))
        html.insert(-2, row("Submitted (UTC)", summary.get("submitted_at_utc")))
        html.insert(-2, row("Submitted (Eastern)", summary.get("submitted_at_eastern")))

    # Results
    total = summary.get("scenarios_total")
    passed = summary.get("scenarios_passed")
    failed = summary.get("scenarios_failed")
    version = summary.get("version") if push_to_github else None
    html += [
        "<h3 style='margin:8px 0;color:#111827'>🎯 Results</h3>",
        "<table style='border-collapse:collapse;width:100%;margin-bottom:12px'>",
        row("Status", summary.get("status")),
        (row("Branch version", version) if version else ""),
        (row("Scenarios", f"✅ {passed} / {total}  |  ❌ {failed} failed") if (total is not None and passed is not None and failed is not None) else ""),
        row("Execution time (s)", int(summary.get("execution_seconds", 0)) if summary.get("execution_seconds") is not None else None),
        "</table>",
    ]

    # GitHub
    if summary.get("github_repo") or summary.get("branch_name") or pr_url or summary.get("pr_number") is not None:
        html += [
            "<h3 style='margin:8px 0;color:#111827'>🐙 GitHub</h3>",
            "<table style='border-collapse:collapse;width:100%;margin-bottom:12px'>",
            row("Repository", summary.get("github_repo")),
            row("Branch", summary.get("branch_name")),
            link_row("Pull Request", pr_url),
            row("PR Number", summary.get("pr_number")),
            "</table>",
        ]

    # Jenkins
    if summary.get("jenkins_project") or summary.get("jenkins_queue") or jenkins_job_url:
        html += [
            "<h3 style='margin:8px 0;color:#111827'>🧰 Jenkins</h3>",
            "<table style='border-collapse:collapse;width:100%;margin-bottom:12px'>",
            row("Job", summary.get("jenkins_project")),
            link_row("Job URL", jenkins_job_url),
            link_row("Queue URL", summary.get("jenkins_queue")),
            "</table>",
        ]

    # Generated package
    if download_zip_filename or download_zip_url or summary.get("jira_id"):
        html += [
            "<h3 style='margin:8px 0;color:#111827'>📦 Generated Package</h3>",
            "<table style='border-collapse:collapse;width:100%;margin-bottom:12px'>",
            row("Scripts ZIP", download_zip_filename or (f"{summary.get('jira_id')}.zip" if summary.get("jira_id") else None)),
            "</table>",
        ]
        # Place the Download Scripts CTA here to avoid confusion with recordings
        if download_zip_url:
            html += [f"<div style='margin:6px 0'>{cta('Download Scripts', download_zip_url, bg='#7c3aed')}</div>"]

    # Recordings
    if recordings:
        html += [
            "<h3 style='margin:8px 0;color:#111827'>🎥 Recordings</h3>",
            "<table style='border-collapse:collapse;width:100%;margin-bottom:12px'>",
        ]
        for rec in recordings:
            scen = rec.get("scenario_number", "?")
            name = rec.get("zip_filename") or rec.get("zip_url") or "recording.zip"
            link = f"{recordings_base}/{name}" if (recordings_base and name) else None
            if link:
                html.append(
                    "<tr>"
                    f"<td style='padding:4px 8px;color:#334155'>Scenario {esc(str(scen))}</td>"
                    f"<td style='padding:4px 8px'>{cta('Download Recording', link, bg='#2563eb')}</td>"
                    "</tr>"
                )
            else:
                html.append(row(f"Scenario {scen}", name))
        html += ["</table>"]

    # Scenario-level friendly reasons (always show if present)
    reasons = summary.get("scenario_failure_reasons") or []
    details = summary.get("scenario_failure_details") or []
    if reasons:
        html += [
            "<div style='background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;padding:10px;margin-top:8px'>",
            "<div style='color:#7c2d12;font-weight:600;margin-bottom:6px'>⚠️ Issues detected</div>",
            "<ul style='margin:0 0 0 18px;color:#111827'>",
        ]
        for r in reasons:
            scen = r.get("scenario_number", "?")
            reason = esc(str(r.get("reason", "Scenario failed")))
            html += [f"<li><strong>Scenario {esc(str(scen))}:</strong> {reason}"]
            # Find technical for this scenario
            technical = None
            try:
                for d in details:
                    if str(d.get("scenario_number")) == str(scen):
                        technical = d.get("technical")
                        break
            except Exception:
                pass
            if technical:
                tech = esc(str(technical))
                if len(tech) > 400:
                    tech = tech[:400] + "…"
                html += [f"<div style='color:#374151;margin-top:4px'>Technical: {tech}</div>"]
            html += ["</li>"]
        html += ["</ul></div>"]

    # Error + Tips (only when overall status is failure)
    if summary.get("status") == "failure":
        if summary.get("error_message") or summary.get("error_stage"):
            # Sanitize error message to remove recording links
            try:
                import re as _re
                emsg = str(summary.get("error_message")) if summary.get("error_message") else None
                if emsg:
                    emsg = _re.sub(r"RECORDING_ZIP:\s*\S+", "", emsg).strip()
            except Exception:
                emsg = summary.get("error_message")
            html += [
                "<div style='background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:10px;margin-top:8px'>",
                "<div style='color:#991b1b;font-weight:600;margin-bottom:6px'>❌ Error</div>",
                (f"<div style='color:#111827'><strong>Message:</strong> {esc(str(emsg))}</div>" if emsg else ""),
                (f"<div style='color:#111827'><strong>Stage:</strong> {esc(str(summary.get('error_stage')))}</div>" if summary.get("error_stage") else ""),
                "</div>",
            ]
        html += [
            "<div style='background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:10px;margin-top:8px'>",
            "<div style='color:#1e3a8a;font-weight:600;margin-bottom:6px'>💡 Tips</div>",
            "<ul style='margin:0 0 0 18px;color:#111827'>",
            "<li>Check the first tag format in the feature file (e.g., payments-ABC123).</li>",
            "<li>Verify GitHub token and repo permissions if PR/branch failed.</li>",
            "<li>Ensure Jenkins credentials and job name are correct if triggering failed.</li>",
            "<li>Review the scenario steps and any API request JSON for syntax issues.</li>",
            "</ul></div>",
        ]

    # CTAs
    ctas = [
        cta("View PR", pr_url),
        cta("Open Jenkins", jenkins_job_url, bg="#059669"),
    ]
    if any(ctas):
        html += ["<div style='margin-top:12px'>" + "".join(ctas) + "</div>"]

    # Close containers
    html += ["</div></div></body></html>"]
    return "".join(html)


def send_email(subject: str, body: str, recipients: List[str], html_body: Optional[str] = None, timeout_seconds: Optional[int] = None) -> None:
    smtp_host = os.getenv("SMTP_HOST") or PLATFORM_CONFIG.get("SMTP_HOST", "localhost")
    smtp_port = int(os.getenv("SMTP_PORT") or PLATFORM_CONFIG.get("SMTP_PORT", 25))
    smtp_sender = os.getenv("SMTP_SENDER") or PLATFORM_CONFIG.get("SMTP_SENDER", "noreply@example.com")
    use_starttls = _get_bool_env("SMTP_STARTTLS", PLATFORM_CONFIG.get("SMTP_STARTTLS", False))
    smtp_user = os.getenv("SMTP_USERNAME") or None
    smtp_pass = os.getenv("SMTP_PASSWORD") or None

    msg = EmailMessage()
    msg["From"] = smtp_sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    # Log connection intent
    try:
        rec_count = len(recipients or [])
        shown_recips = ", ".join((recipients or [])[:3])
        if rec_count > 3:
            shown_recips = f"{shown_recips}, +{rec_count-3} more"
        logger.info(f"Email: connecting host={smtp_host} port={smtp_port} starttls={use_starttls} sender={smtp_sender} auth={'yes' if smtp_user else 'no'} recipients={rec_count} [{shown_recips}]")
    except Exception:
        # Best-effort logging; do not block send
        pass

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=(timeout_seconds or 20)) as smtp:
            logger.info("Email: connected to SMTP server")
            if use_starttls:
                try:
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
                    logger.info("Email: STARTTLS negotiated successfully")
                except Exception as tls_exc:
                    logger.error(f"Email: STARTTLS failed: {type(tls_exc).__name__}: {str(tls_exc)}")
                    raise
            if smtp_user:
                try:
                    logger.info(f"Email: logging in as {smtp_user}")
                    smtp.login(smtp_user, smtp_pass or "")
                    logger.info("Email: authenticated successfully")
                except Exception as auth_exc:
                    code = getattr(auth_exc, "smtp_code", "-")
                    err = getattr(auth_exc, "smtp_error", "-")
                    logger.error(f"Email: authentication failed: {type(auth_exc).__name__} code={code} error={err}")
                    raise
            try:
                logger.info(f"Email: sending message subject='{subject}' to={rec_count} recipient(s)")
            except Exception:
                pass
            smtp.send_message(msg)
            logger.info(f"Email: message sent successfully to {rec_count} recipient(s)")
        logger.debug("Email: SMTP session closed")
    except smtplib.SMTPRecipientsRefused as rf_exc:
        try:
            refused = getattr(rf_exc, "recipients", {}) or {}
            for r, info in refused.items():
                try:
                    logger.error(f"Email: recipient refused: {r} -> {info}")
                except Exception:
                    pass
        finally:
            raise
    except smtplib.SMTPResponseException as rsp_exc:
        logger.error(f"Email: send/connect failed: code={rsp_exc.smtp_code} error={rsp_exc.smtp_error}")
        raise
    except Exception as gen_exc:
        logger.error(f"Email: failure: {type(gen_exc).__name__}: {str(gen_exc)}")
        raise


def send_run_summary_email(summary: Dict[str, Any], recipients: Optional[List[str]] = None, timeout_seconds: Optional[int] = None) -> None:
    recipients = _dedupe_recipients(recipients)
    try:
        logger.info(f"Email: preparing run summary | status={summary.get('status')} jira={summary.get('jira_id')} domain={summary.get('selected_domain')} run_id={summary.get('run_id')} recipients={len(recipients or [])}")
    except Exception:
        pass
    if not recipients:
        logger.warning("Email: skipped run summary (no recipients)")
        return
    subject = _format_summary_subject(summary)
    body = _format_summary_body(summary)
    html_body = _format_summary_html(summary)
    send_email(subject, body, recipients, html_body, timeout_seconds=timeout_seconds)


