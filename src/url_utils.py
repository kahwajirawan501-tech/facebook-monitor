import re
from urllib.parse import urlparse, parse_qs

def clean_facebook_url(url: str) -> str:
    """تنظيف وتوحيد روابط الفيسبوك وإزالة المتتبعات الزائدة."""
    if not url:
        return ""
    
    # إزالة التبعات والرموز الزائدة
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    
    # الاحتفاظ بمعرفات المنشور الضرورية إن وجدت في query parameters
    query_params = parse_qs(parsed.query)
    preserved_params = []
    
    for param in ['story_fbid', 'fbid', 'id', 'post_id']:
        if param in query_params:
            preserved_params.append(f"{param}={query_params[param][0]}")
            
    if preserved_params:
        clean_url += "?" + "&".join(preserved_params)
        
    return clean_url

def is_valid_post_url(url: str) -> bool:
    """التحقق من أن الرابط هو رابط منشور رئيسي صالح وليس تعليقاً جانبياً."""
    if not url:
        return False
    
    # استبعاد روابط التعليقات بشكل قاطع لئلا يتسبب في خطأ الإرسال
    if "comment_id=" in url or "user/" in url:
        return False
        
    # السماح بالروابط التي تحتوي على مسارات ومعرفات المنشورات المعتمدة
    valid_patterns = ["/posts/", "fbid=", "/permalink.php", "story.php", "/videos/"]
    return any(pattern in url for pattern in valid_patterns)
