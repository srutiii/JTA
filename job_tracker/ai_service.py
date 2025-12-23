"""
AI Service Module - Centralized Google Gemini API Integration
Handles all AI-powered features: CV extraction, cover letters, emails, matching
"""

import json
import os
import re
from typing import Dict, Any, Optional

# Google Gemini API imports
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# Configuration
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_CONFIGURED = False

if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        GEMINI_CONFIGURED = True
    except Exception:
        GEMINI_CONFIGURED = False


def is_ai_available() -> bool:
    """Check if Gemini AI is available and configured."""
    return GEMINI_AVAILABLE and GEMINI_CONFIGURED


def log_prompt_and_response(prompt: str, response: str, feature: str) -> None:
    """Log prompts and responses for debugging (can be extended to write to file)."""
    print(f"[AI {feature}] Prompt length: {len(prompt)} chars")
    print(f"[AI {feature}] Response length: {len(response)} chars")
    # In production, you might want to log to a file or database


def extract_cv_data_deep(cv_text: str) -> Dict[str, Any]:
    """
    Deep CV extraction using Google Gemini API.
    Extracts ALL professional information for complete About Me profile.
    
    Rules:
    - Extract everything a recruiter would expect
    - Do NOT limit to predefined sections
    - Capture identity, intent, strengths, and career narrative
    - If data is missing → return null or empty arrays
    - No guessing, no filler text
    """
    if not is_ai_available():
        return _get_empty_profile_structure()
    
    if not cv_text or len(cv_text.strip()) < 50:
        return _get_empty_profile_structure()
    
    try:
        model = genai.GenerativeModel('gemini-pro')
        
        # Limit text to 8000 chars for API
        cv_text_limited = cv_text[:8000] if len(cv_text) > 8000 else cv_text
        
        prompt = f"""Extract COMPLETE professional information from this CV/resume. Extract ALL information that a recruiter would expect in a professional "About Me" profile.

CV TEXT:
{cv_text_limited}

EXTRACTION REQUIREMENTS:
Extract ALL of the following, even if some sections are missing:

1. IDENTITY:
   - Full Name
   - Email
   - Phone (if present)
   - Location (city/country if present)
   - LinkedIn, GitHub, Portfolio links (if present)

2. CAREER INTENT:
   - Current role/status (e.g., "Software Engineer", "Student", "Fresher")
   - Target roles (array, e.g., ["Software Engineer", "Frontend Developer"])
   - Industry preference (if inferable, else null)

3. PROFESSIONAL SUMMARY:
   - Rewrite into clean 3-5 line recruiter-friendly bio
   - Use ONLY CV content
   - Do NOT hallucinate

4. SKILLS (categorized):
   - Technical Skills (programming languages, technologies)
   - Tools & Frameworks
   - Soft Skills (ONLY if explicitly mentioned in CV)

5. EXPERIENCE (array of objects):
   - Company name
   - Role/title
   - Duration (if present)
   - Key responsibilities (bulleted, concise)

6. EDUCATION (array of objects):
   - Degree
   - Institution
   - Year (if present)
   - Specialization (if present)

7. PROJECTS (array of objects):
   - Project name
   - Tech stack
   - One-line impact summary

8. ACHIEVEMENTS/CERTIFICATIONS:
   - Only if mentioned in CV

CRITICAL RULES:
1. Return ONLY valid JSON, no markdown, no code blocks
2. If data is missing → use null or empty arrays []
3. Do NOT invent or hallucinate any values
4. Do NOT add filler text
5. Extract everything that exists, nothing that doesn't
6. Keep all text professional and recruiter-friendly

OUTPUT FORMAT (JSON only):
{{
  "identity": {{
    "name": "",
    "email": "",
    "phone": "",
    "location": "",
    "links": []
  }},
  "career_intent": {{
    "current_status": "",
    "target_roles": [],
    "industry": ""
  }},
  "professional_summary": "",
  "skills": {{
    "technical": [],
    "tools": [],
    "soft": []
  }},
  "experience": [],
  "education": [],
  "projects": [],
  "achievements": []
}}"""
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,  # Low temperature for deterministic extraction
                max_output_tokens=4000,
            )
        )
        
        response_text = response.text.strip()
        log_prompt_and_response(prompt, response_text, "DEEP_CV_EXTRACTION")
        
        # Remove markdown code blocks if present
        if response_text.startswith('```'):
            response_text = response_text.split('```')[1]
            if response_text.startswith('json'):
                response_text = response_text[4:]
            response_text = response_text.strip()
        elif response_text.startswith('```json'):
            response_text = response_text[7:].strip()
            if response_text.endswith('```'):
                response_text = response_text[:-3].strip()
        
        # Parse JSON
        try:
            extracted_data = json.loads(response_text)
        except json.JSONDecodeError:
            json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
            if json_match:
                extracted_data = json.loads(json_match.group(0))
            else:
                raise ValueError("Could not parse JSON from Gemini response")
        
        # Validate and normalize structure
        return _normalize_profile_structure(extracted_data)
    
    except Exception as e:
        print(f"Error in deep CV extraction: {e}")
        return _get_empty_profile_structure()


def _get_empty_profile_structure() -> Dict[str, Any]:
    """Return empty profile structure."""
    return {
        "identity": {
            "name": "",
            "email": "",
            "phone": "",
            "location": "",
            "links": []
        },
        "career_intent": {
            "current_status": "",
            "target_roles": [],
            "industry": ""
        },
        "professional_summary": "",
        "skills": {
            "technical": [],
            "tools": [],
            "soft": []
        },
        "experience": [],
        "education": [],
        "projects": [],
        "achievements": []
    }


def _normalize_profile_structure(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and validate extracted profile structure."""
    result = _get_empty_profile_structure()
    
    # Identity
    if "identity" in data and isinstance(data["identity"], dict):
        identity = data["identity"]
        result["identity"] = {
            "name": str(identity.get("name", "")).strip() if identity.get("name") else "",
            "email": str(identity.get("email", "")).strip() if identity.get("email") else "",
            "phone": str(identity.get("phone", "")).strip() if identity.get("phone") else "",
            "location": str(identity.get("location", "")).strip() if identity.get("location") else "",
            "links": [str(link).strip() for link in identity.get("links", []) if link] if isinstance(identity.get("links"), list) else []
        }
    
    # Career Intent
    if "career_intent" in data and isinstance(data["career_intent"], dict):
        intent = data["career_intent"]
        result["career_intent"] = {
            "current_status": str(intent.get("current_status", "")).strip() if intent.get("current_status") else "",
            "target_roles": [str(role).strip() for role in intent.get("target_roles", []) if role] if isinstance(intent.get("target_roles"), list) else [],
            "industry": str(intent.get("industry", "")).strip() if intent.get("industry") else ""
        }
    
    # Professional Summary
    if data.get("professional_summary"):
        result["professional_summary"] = str(data["professional_summary"]).strip()
    
    # Skills
    if "skills" in data and isinstance(data["skills"], dict):
        skills = data["skills"]
        result["skills"] = {
            "technical": [str(s).strip() for s in skills.get("technical", []) if s] if isinstance(skills.get("technical"), list) else [],
            "tools": [str(s).strip() for s in skills.get("tools", []) if s] if isinstance(skills.get("tools"), list) else [],
            "soft": [str(s).strip() for s in skills.get("soft", []) if s] if isinstance(skills.get("soft"), list) else []
        }
    
    # Experience
    if "experience" in data and isinstance(data["experience"], list):
        result["experience"] = [
            {
                "company": str(exp.get("company", "")).strip() if exp.get("company") else "",
                "role": str(exp.get("role", "")).strip() if exp.get("role") else "",
                "duration": str(exp.get("duration", "")).strip() if exp.get("duration") else "",
                "responsibilities": [str(r).strip() for r in exp.get("responsibilities", []) if r] if isinstance(exp.get("responsibilities"), list) else []
            }
            for exp in data["experience"] if isinstance(exp, dict)
        ]
    
    # Education
    if "education" in data and isinstance(data["education"], list):
        result["education"] = [
            {
                "degree": str(edu.get("degree", "")).strip() if edu.get("degree") else "",
                "institution": str(edu.get("institution", "")).strip() if edu.get("institution") else "",
                "year": str(edu.get("year", "")).strip() if edu.get("year") else "",
                "specialization": str(edu.get("specialization", "")).strip() if edu.get("specialization") else ""
            }
            for edu in data["education"] if isinstance(edu, dict)
        ]
    
    # Projects
    if "projects" in data and isinstance(data["projects"], list):
        result["projects"] = [
            {
                "name": str(proj.get("name", "")).strip() if proj.get("name") else "",
                "tech_stack": str(proj.get("tech_stack", "")).strip() if proj.get("tech_stack") else "",
                "impact": str(proj.get("impact", "")).strip() if proj.get("impact") else ""
            }
            for proj in data["projects"] if isinstance(proj, dict)
        ]
    
    # Achievements
    if "achievements" in data:
        if isinstance(data["achievements"], list):
            result["achievements"] = [str(a).strip() for a in data["achievements"] if a]
        elif isinstance(data["achievements"], str):
            result["achievements"] = [data["achievements"].strip()] if data["achievements"].strip() else []
    
    return result


def extract_cv_data(cv_text: str) -> Dict[str, Any]:
    """
    Legacy function - redirects to deep extraction.
    Kept for backward compatibility.
    """
    return extract_cv_data_deep(cv_text)
    if not is_ai_available():
        return {
            "name": "",
            "email": "",
            "phone": "",
            "looking_for": "",
            "bio": "",
            "skills": [],
            "experience_summary": ""
        }
    
    if not cv_text or len(cv_text.strip()) < 50:
        return {
            "name": "",
            "email": "",
            "phone": "",
            "looking_for": "",
            "bio": "",
            "skills": [],
            "experience_summary": ""
        }
    
    try:
        model = genai.GenerativeModel('gemini-pro')
        
        # Limit text to 8000 chars for API
        cv_text_limited = cv_text[:8000] if len(cv_text) > 8000 else cv_text
        
        prompt = f"""Extract structured information from this CV/resume text. Return ONLY valid JSON, no explanations.

CV Text:
{cv_text_limited}

Extract and return the following fields as JSON:
- name: Full name (string, or "" if not found)
- email: Email address (string, or "" if not found)
- phone: Phone number (string, or "" if not found)
- looking_for: Current or target role/job title (string, or "" if not found)
- bio: Professional summary in 2-4 lines, recruiter-friendly tone (string, or "" if not found)
- skills: Array of technical/professional skills (array of strings, or [] if not found)
- experience_summary: Brief experience summary (string, or "" if not found)

CRITICAL RULES:
1. Return ONLY valid JSON, no markdown, no code blocks
2. If a field is not found in the CV, use empty string "" or empty array []
3. Do NOT invent or hallucinate any values
4. For bio: Only create if there's a summary/objective section in the CV
5. For skills: Extract actual skills mentioned, return as array
6. Keep all text professional and recruiter-friendly

Return format (JSON only):
{{
  "name": "",
  "email": "",
  "phone": "",
  "looking_for": "",
  "bio": "",
  "skills": [],
  "experience_summary": ""
}}"""
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,  # Low temperature for deterministic extraction
                max_output_tokens=2000,
            )
        )
        
        response_text = response.text.strip()
        log_prompt_and_response(prompt, response_text, "CV_EXTRACTION")
        
        # Remove markdown code blocks if present
        if response_text.startswith('```'):
            response_text = response_text.split('```')[1]
            if response_text.startswith('json'):
                response_text = response_text[4:]
            response_text = response_text.strip()
        elif response_text.startswith('```json'):
            response_text = response_text[7:].strip()
            if response_text.endswith('```'):
                response_text = response_text[:-3].strip()
        
        # Parse JSON
        try:
            extracted_data = json.loads(response_text)
        except json.JSONDecodeError:
            # Try to extract JSON from the response
            json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
            if json_match:
                extracted_data = json.loads(json_match.group(0))
            else:
                raise ValueError("Could not parse JSON from Gemini response")
        
        # Validate and normalize the response
        result = {
            "name": str(extracted_data.get("name", "")).strip() if extracted_data.get("name") else "",
            "email": str(extracted_data.get("email", "")).strip() if extracted_data.get("email") else "",
            "phone": str(extracted_data.get("phone", "")).strip() if extracted_data.get("phone") else "",
            "looking_for": str(extracted_data.get("looking_for", "")).strip() if extracted_data.get("looking_for") else "",
            "bio": str(extracted_data.get("bio", "")).strip() if extracted_data.get("bio") else "",
            "skills": extracted_data.get("skills", []) if isinstance(extracted_data.get("skills"), list) else [],
            "experience_summary": str(extracted_data.get("experience_summary", "")).strip() if extracted_data.get("experience_summary") else ""
        }
        
        # Clean skills array
        if result["skills"]:
            result["skills"] = [str(skill).strip() for skill in result["skills"] if skill and str(skill).strip()]
        
        return result
    
    except Exception as e:
        print(f"Error in Gemini CV extraction: {e}")
        return {
            "name": "",
            "email": "",
            "phone": "",
            "looking_for": "",
            "bio": "",
            "skills": [],
            "experience_summary": ""
        }


def generate_cover_letter(about_me: Dict[str, Any], job_description: str, company_name: str, role_title: str) -> str:
    """
    Generate a customized, professional cover letter using Gemini AI.
    
    Args:
        about_me: Structured profile data from database
        job_description: Job description text
        company_name: Company name
        role_title: Job role/title
    
    Returns:
        Plain text cover letter (250-300 words)
    """
    if not is_ai_available():
        return "AI service not available. Please configure GEMINI_API_KEY."
    
    try:
        model = genai.GenerativeModel('gemini-pro')
        
        # Prepare about me summary
        about_me_text = f"""
Name: {about_me.get('name', 'N/A')}
Target Role: {about_me.get('looking_for', 'N/A')}
Professional Summary: {about_me.get('bio', 'N/A')}
Skills: {', '.join(about_me.get('skills', [])) if isinstance(about_me.get('skills'), list) else about_me.get('skills', 'N/A')}
Experience: {about_me.get('experience_summary', about_me.get('experience', 'N/A'))}
"""
        
        prompt = f"""Generate a professional cover letter for a job application.

CANDIDATE INFORMATION:
{about_me_text}

JOB DETAILS:
Company: {company_name}
Role: {role_title}

JOB DESCRIPTION:
{job_description[:2000]}

INSTRUCTIONS:
1. Write a professional cover letter (250-300 words)
2. Align candidate's skills with the job requirements
3. Show genuine interest in the role and company
4. Avoid buzzwords, clichés, and exaggeration
5. Use confident, human, recruiter-friendly tone
6. Be specific about relevant experience
7. Keep it concise and impactful

TONE:
- Confident but not arrogant
- Human and authentic
- Professional and recruiter-friendly
- No clichés like "I'm a team player" or "I'm passionate"

OUTPUT:
Return ONLY the cover letter text, no greetings, no explanations, no markdown formatting.
Start directly with the salutation (e.g., "Dear Hiring Manager,").
"""
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,  # Slightly higher for creative writing
                max_output_tokens=500,
            )
        )
        
        cover_letter = response.text.strip()
        log_prompt_and_response(prompt, cover_letter, "COVER_LETTER")
        
        # Clean up the response
        if cover_letter.startswith('```'):
            cover_letter = cover_letter.split('```')[1]
            if cover_letter.startswith('text'):
                cover_letter = cover_letter[4:]
            cover_letter = cover_letter.strip()
        
        return cover_letter
    
    except Exception as e:
        print(f"Error generating cover letter: {e}")
        return f"Error generating cover letter: {str(e)}"


def generate_application_email(
    candidate_name: str,
    role: str,
    company: str,
    resume_attached: bool = True,
    cover_letter: Optional[str] = None
) -> Dict[str, str]:
    """
    Generate a professional job application email.
    
    Args:
        candidate_name: Candidate's name
        role: Job role/title
        company: Company name
        resume_attached: Whether resume is attached
        cover_letter: Optional cover letter text
    
    Returns:
        Dictionary with 'subject' and 'body' keys
    """
    if not is_ai_available():
        return {
            "subject": f"Application for {role} Position",
            "body": "AI service not available. Please configure GEMINI_API_KEY."
        }
    
    try:
        model = genai.GenerativeModel('gemini-pro')
        
        cover_letter_note = f"\nCover letter: {'Included' if cover_letter else 'Not included'}"
        if cover_letter:
            cover_letter_note += f"\nCover letter preview: {cover_letter[:200]}..."
        
        prompt = f"""Generate a professional job application email.

CANDIDATE NAME: {candidate_name}
ROLE: {role}
COMPANY: {company}
RESUME ATTACHED: {'Yes' if resume_attached else 'No'}{cover_letter_note}

INSTRUCTIONS:
1. Generate an email subject line (concise, professional)
2. Generate an email body (concise, professional)
3. Suitable for cold outreach or referral submission
4. No emojis
5. No excessive formality
6. Clear call-to-action
7. Professional but approachable tone
8. Mention the role and company
9. Reference attached resume if applicable

OUTPUT FORMAT (JSON only):
{{
  "subject": "Email subject line",
  "body": "Email body text"
}}

Return ONLY valid JSON, no markdown, no explanations.
"""
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.6,
                max_output_tokens=400,
            )
        )
        
        response_text = response.text.strip()
        log_prompt_and_response(prompt, response_text, "APPLICATION_EMAIL")
        
        # Parse JSON
        if response_text.startswith('```'):
            response_text = response_text.split('```')[1]
            if response_text.startswith('json'):
                response_text = response_text[4:]
            response_text = response_text.strip()
        elif response_text.startswith('```json'):
            response_text = response_text[7:].strip()
            if response_text.endswith('```'):
                response_text = response_text[:-3].strip()
        
        try:
            email_data = json.loads(response_text)
        except json.JSONDecodeError:
            json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
            if json_match:
                email_data = json.loads(json_match.group(0))
            else:
                raise ValueError("Could not parse JSON from Gemini response")
        
        return {
            "subject": str(email_data.get("subject", f"Application for {role} Position")).strip(),
            "body": str(email_data.get("body", "")).strip()
        }
    
    except Exception as e:
        print(f"Error generating application email: {e}")
        return {
            "subject": f"Application for {role} Position",
            "body": f"Dear Hiring Manager,\n\nI am writing to express my interest in the {role} position at {company}.\n\nPlease find my resume attached.\n\nBest regards,\n{candidate_name}"
        }


def match_jd_cv(job_description: str, about_me: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze JD-CV match and return scoring.
    
    Args:
        job_description: Job description text
        about_me: Structured profile data from database
    
    Returns:
        Dictionary with match_score, matched_skills, missing_skills, summary
    """
    if not is_ai_available():
        return {
            "match_score": 0,
            "matched_skills": [],
            "missing_skills": [],
            "summary": "AI service not available for matching."
        }
    
    try:
        model = genai.GenerativeModel('gemini-pro')
        
        # Prepare candidate profile
        candidate_skills = about_me.get('skills', [])
        if isinstance(candidate_skills, str):
            candidate_skills = [s.strip() for s in candidate_skills.split(',') if s.strip()]
        
        candidate_profile = f"""
Name: {about_me.get('name', 'N/A')}
Target Role: {about_me.get('looking_for', 'N/A')}
Professional Summary: {about_me.get('bio', 'N/A')}
Skills: {', '.join(candidate_skills) if candidate_skills else 'N/A'}
Experience: {about_me.get('experience_summary', about_me.get('experience', 'N/A'))}
"""
        
        prompt = f"""Analyze the match between a job description and a candidate's profile.

JOB DESCRIPTION:
{job_description[:2000]}

CANDIDATE PROFILE:
{candidate_profile}

TASK:
1. Analyze relevance and compatibility
2. Match skills mentioned in JD with candidate's skills
3. Identify missing skills that the JD requires
4. Calculate a match score (0-100) based on:
   - Skills alignment (40%)
   - Experience relevance (30%)
   - Role fit (20%)
   - Overall compatibility (10%)
5. Provide a brief summary (2-3 sentences)

OUTPUT FORMAT (JSON only):
{{
  "match_score": 0-100,
  "matched_skills": ["skill1", "skill2", ...],
  "missing_skills": ["skill1", "skill2", ...],
  "summary": "Brief summary of the match"
}}

CRITICAL RULES:
1. Return ONLY valid JSON, no markdown, no code blocks
2. match_score must be an integer between 0 and 100
3. matched_skills: array of skills that appear in both JD and candidate profile
4. missing_skills: array of important skills from JD that candidate lacks
5. summary: 2-3 sentences explaining the match quality
6. Be honest and accurate - don't inflate scores
"""
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,  # Low temperature for analytical task
                max_output_tokens=500,
            )
        )
        
        response_text = response.text.strip()
        log_prompt_and_response(prompt, response_text, "JD_CV_MATCHING")
        
        # Parse JSON
        if response_text.startswith('```'):
            response_text = response_text.split('```')[1]
            if response_text.startswith('json'):
                response_text = response_text[4:]
            response_text = response_text.strip()
        elif response_text.startswith('```json'):
            response_text = response_text[7:].strip()
            if response_text.endswith('```'):
                response_text = response_text[:-3].strip()
        
        try:
            match_data = json.loads(response_text)
        except json.JSONDecodeError:
            json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
            if json_match:
                match_data = json.loads(json_match.group(0))
            else:
                raise ValueError("Could not parse JSON from Gemini response")
        
        # Validate and normalize
        match_score = int(match_data.get("match_score", 0))
        match_score = max(0, min(100, match_score))  # Clamp between 0-100
        
        matched_skills = match_data.get("matched_skills", [])
        if not isinstance(matched_skills, list):
            matched_skills = []
        
        missing_skills = match_data.get("missing_skills", [])
        if not isinstance(missing_skills, list):
            missing_skills = []
        
        return {
            "match_score": match_score,
            "matched_skills": [str(s).strip() for s in matched_skills if s],
            "missing_skills": [str(s).strip() for s in missing_skills if s],
            "summary": str(match_data.get("summary", "")).strip()
        }
    
    except Exception as e:
        print(f"Error in JD-CV matching: {e}")
        return {
            "match_score": 0,
            "matched_skills": [],
            "missing_skills": [],
            "summary": f"Error analyzing match: {str(e)}"
        }

