"""
AI Service Module - Centralized Google Gemini API Integration
Handles all AI-powered features: CV extraction, cover letters, emails, matching
"""

import json
import os
import re
from typing import Dict, Any, Optional

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv not installed, continue without it
    pass

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
        model = genai.GenerativeModel('models/gemini-1.5-pro')
        
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

2. PROFESSIONAL SUMMARY:
   - Rewrite into clean 3-5 line recruiter-friendly bio
   - Use ONLY CV content
   - Do NOT hallucinate

3. SKILLS (categorized):
   - Technical Skills (programming languages, technologies)
   - Tools & Frameworks
   - Soft Skills (ONLY if explicitly mentioned in CV)

4. EXPERIENCE (array of objects):
   - Company name
   - Role/title
   - Duration (if present)
   - Key responsibilities (bulleted, concise)

5. EDUCATION (array of objects):
   - Degree
   - Institution
   - Year (if present)
   - Specialization (if present)

6. PROJECTS (array of objects):
   - Project name
   - Tech stack
   - One-line impact summary

7. ACHIEVEMENTS/CERTIFICATIONS:
   - Only if mentioned in CV

CRITICAL RULES:
1. Return ONLY valid JSON, no markdown, no code blocks
2. If a value is NOT FOUND in the CV, use null (NOT empty strings "" or empty arrays [])
3. Only include actual data found in the CV
4. Do NOT invent or hallucinate any values
5. Do NOT add filler text
6. Extract everything that exists, nothing that doesn't
7. Keep all text professional and recruiter-friendly
8. For arrays: use null if no items found, otherwise return array with items
9. For strings: use null if not found, otherwise return the actual string value

OUTPUT FORMAT (JSON only - STRICT STRUCTURE):
{{
  "identity": {{
    "name": null,
    "email": null,
    "phone": null,
    "location": null,
    "links": null
  }},
  "professional_summary": null,
  "skills": {{
    "technical": null,
    "tools": null,
    "soft": null
  }},
  "experience": null,
  "education": null,
  "projects": null,
  "achievements": null
}}

IMPORTANT: Replace null with actual values ONLY if found in the CV. If not found, keep as null."""
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,  # Low temperature for deterministic extraction
                max_output_tokens=4000,
            )
        )
        
        # Log raw response before any processing
        raw_response_text = response.text if hasattr(response, 'text') else str(response)
        print(f"[INFO] Raw Gemini response (DEEP_CV_EXTRACTION):")
        print(f"[RAW_RESPONSE] {raw_response_text}")
        print(f"[INFO] Raw response length: {len(raw_response_text)} chars")
        
        response_text = raw_response_text.strip()
        log_prompt_and_response(prompt, response_text, "DEEP_CV_EXTRACTION")
        
        print(f"[INFO] Processed response length: {len(response_text)} chars")
        
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
            print(f"[INFO] Successfully parsed JSON from Gemini response")
        except json.JSONDecodeError as e:
            print(f"[WARNING] JSON parse error: {e}")
            print(f"[DEBUG] Response text (first 500 chars): {response_text[:500]}")
            # Try to extract JSON from the response
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    extracted_data = json.loads(json_match.group(0))
                    print(f"[INFO] Successfully extracted JSON using regex")
                except json.JSONDecodeError:
                    raise ValueError(f"Could not parse JSON from Gemini response: {e}")
            else:
                raise ValueError(f"Could not find JSON in Gemini response: {e}")
        
        # Validate that we got some data
        if not extracted_data or not isinstance(extracted_data, dict):
            print(f"[ERROR] Extracted data is not a valid dict: {type(extracted_data)}")
            return _get_empty_profile_structure()
        
        # Log what was extracted
        print(f"[INFO] Extracted data keys: {list(extracted_data.keys())}")
        identity_data = extracted_data.get("identity", {})
        if identity_data and isinstance(identity_data, dict):
            name_val = identity_data.get("name")
            print(f"[INFO] Identity name extracted: {name_val is not None and name_val != ''}")
        prof_summary = extracted_data.get("professional_summary")
        if prof_summary is not None and prof_summary != "":
            print(f"[INFO] Professional summary extracted: {len(str(prof_summary))} chars")
        experience_data = extracted_data.get("experience")
        if experience_data is not None and isinstance(experience_data, list):
            print(f"[INFO] Experience entries: {len(experience_data)}")
        skills_data = extracted_data.get("skills", {})
        if skills_data and isinstance(skills_data, dict):
            technical_skills = skills_data.get("technical")
            if technical_skills is not None and isinstance(technical_skills, list):
                print(f"[INFO] Skills extracted: {len(technical_skills)} technical")
        
        # Validate and normalize structure
        normalized = _normalize_profile_structure(extracted_data)
        
        # Final validation - check if we got any meaningful data
        identity = normalized.get("identity", {})
        skills = normalized.get("skills", {})
        experience = normalized.get("experience")
        has_data = (
            (identity.get("name") is not None and identity.get("name") != "") or
            (normalized.get("professional_summary") is not None and normalized.get("professional_summary") != "") or
            (experience is not None and isinstance(experience, list) and len(experience) > 0) or
            (skills.get("technical") is not None and isinstance(skills.get("technical"), list) and len(skills.get("technical", [])) > 0)
        )
        
        if not has_data:
            print(f"[WARNING] Extracted data appears to be empty after normalization (all values are null)")
        
        return normalized
    
    except Exception as e:
        print(f"Error in deep CV extraction: {e}")
        return _get_empty_profile_structure()


def _get_empty_profile_structure() -> Dict[str, Any]:
    """Return empty profile structure with null values."""
    return {
        "identity": {
            "name": None,
            "email": None,
            "phone": None,
            "location": None,
            "links": None
        },
        "professional_summary": None,
        "skills": {
            "technical": None,
            "tools": None,
            "soft": None
        },
        "experience": None,
        "education": None,
        "projects": None,
        "achievements": None
    }


def _normalize_profile_structure(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and validate extracted profile structure. Preserves null values."""
    result = _get_empty_profile_structure()
    
    # Identity
    if "identity" in data and isinstance(data["identity"], dict):
        identity = data["identity"]
        result["identity"] = {
            "name": str(identity.get("name")).strip() if identity.get("name") is not None and identity.get("name") != "" else None,
            "email": str(identity.get("email")).strip() if identity.get("email") is not None and identity.get("email") != "" else None,
            "phone": str(identity.get("phone")).strip() if identity.get("phone") is not None and identity.get("phone") != "" else None,
            "location": str(identity.get("location")).strip() if identity.get("location") is not None and identity.get("location") != "" else None,
            "links": [str(link).strip() for link in identity.get("links") if link] if isinstance(identity.get("links"), list) and identity.get("links") is not None else None
        }
    
    # Professional Summary
    if "professional_summary" in data:
        prof_summary = data.get("professional_summary")
        result["professional_summary"] = str(prof_summary).strip() if prof_summary is not None and prof_summary != "" else None
    
    # Skills
    if "skills" in data and isinstance(data["skills"], dict):
        skills = data["skills"]
        result["skills"] = {
            "technical": [str(s).strip() for s in skills.get("technical") if s] if isinstance(skills.get("technical"), list) and skills.get("technical") is not None else None,
            "tools": [str(s).strip() for s in skills.get("tools") if s] if isinstance(skills.get("tools"), list) and skills.get("tools") is not None else None,
            "soft": [str(s).strip() for s in skills.get("soft") if s] if isinstance(skills.get("soft"), list) and skills.get("soft") is not None else None
        }
    
    # Experience
    if "experience" in data:
        if isinstance(data["experience"], list) and data["experience"] is not None:
            result["experience"] = [
                {
                    "company": str(exp.get("company")).strip() if exp.get("company") is not None and exp.get("company") != "" else None,
                    "role": str(exp.get("role")).strip() if exp.get("role") is not None and exp.get("role") != "" else None,
                    "duration": str(exp.get("duration")).strip() if exp.get("duration") is not None and exp.get("duration") != "" else None,
                    "responsibilities": [str(r).strip() for r in exp.get("responsibilities") if r] if isinstance(exp.get("responsibilities"), list) and exp.get("responsibilities") is not None else None
                }
                for exp in data["experience"] if isinstance(exp, dict)
            ]
        else:
            result["experience"] = None
    
    # Education
    if "education" in data:
        if isinstance(data["education"], list) and data["education"] is not None:
            result["education"] = [
                {
                    "degree": str(edu.get("degree")).strip() if edu.get("degree") is not None and edu.get("degree") != "" else None,
                    "institution": str(edu.get("institution")).strip() if edu.get("institution") is not None and edu.get("institution") != "" else None,
                    "year": str(edu.get("year")).strip() if edu.get("year") is not None and edu.get("year") != "" else None,
                    "specialization": str(edu.get("specialization")).strip() if edu.get("specialization") is not None and edu.get("specialization") != "" else None
                }
                for edu in data["education"] if isinstance(edu, dict)
            ]
        else:
            result["education"] = None
    
    # Projects
    if "projects" in data:
        if isinstance(data["projects"], list) and data["projects"] is not None:
            result["projects"] = [
                {
                    "name": str(proj.get("name")).strip() if proj.get("name") is not None and proj.get("name") != "" else None,
                    "tech_stack": str(proj.get("tech_stack")).strip() if proj.get("tech_stack") is not None and proj.get("tech_stack") != "" else None,
                    "impact": str(proj.get("impact")).strip() if proj.get("impact") is not None and proj.get("impact") != "" else None
                }
                for proj in data["projects"] if isinstance(proj, dict)
            ]
        else:
            result["projects"] = None
    
    # Achievements
    if "achievements" in data:
        if isinstance(data["achievements"], list) and data["achievements"] is not None:
            result["achievements"] = [str(a).strip() for a in data["achievements"] if a] if len([a for a in data["achievements"] if a]) > 0 else None
        elif isinstance(data["achievements"], str) and data["achievements"] is not None:
            result["achievements"] = [data["achievements"].strip()] if data["achievements"].strip() else None
        else:
            result["achievements"] = None
    
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
        model = genai.GenerativeModel('models/gemini-1.5-pro')
        
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
        
        # Log raw response before any processing
        raw_response_text = response.text if hasattr(response, 'text') else str(response)
        print(f"[INFO] Raw Gemini response (CV_EXTRACTION):")
        print(f"[RAW_RESPONSE] {raw_response_text}")
        print(f"[INFO] Raw response length: {len(raw_response_text)} chars")
        
        response_text = raw_response_text.strip()
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
        model = genai.GenerativeModel('models/gemini-1.5-pro')
        
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
        
        # Log raw response before any processing
        raw_response_text = response.text if hasattr(response, 'text') else str(response)
        print(f"[INFO] Raw Gemini response (COVER_LETTER):")
        print(f"[RAW_RESPONSE] {raw_response_text}")
        print(f"[INFO] Raw response length: {len(raw_response_text)} chars")
        
        cover_letter = raw_response_text.strip()
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
        model = genai.GenerativeModel('models/gemini-1.5-pro')
        
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
        
        # Log raw response before any processing
        raw_response_text = response.text if hasattr(response, 'text') else str(response)
        print(f"[INFO] Raw Gemini response (APPLICATION_EMAIL):")
        print(f"[RAW_RESPONSE] {raw_response_text}")
        print(f"[INFO] Raw response length: {len(raw_response_text)} chars")
        
        response_text = raw_response_text.strip()
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
        model = genai.GenerativeModel('models/gemini-1.5-pro')
        
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
        
        # Log raw response before any processing
        raw_response_text = response.text if hasattr(response, 'text') else str(response)
        print(f"[INFO] Raw Gemini response (JD_CV_MATCHING):")
        print(f"[RAW_RESPONSE] {raw_response_text}")
        print(f"[INFO] Raw response length: {len(raw_response_text)} chars")
        
        response_text = raw_response_text.strip()
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

