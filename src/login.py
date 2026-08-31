import asyncio
from playwright.async_api import async_playwright

async def save_fb_state():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        await page.goto("https://www.facebook.com")
        print("⏳ قم بتسجيل الدخول يدويّاً في متصفح فيسبوك...")
        print("⌨️ عندما تنتهي تماماً وتظهر لك الصفحة الرئيسية، اضغط على مفتاح (Enter) هنا في التيرمينال للحفظ الفوري...")
        
        # الانتظار حتى تقوم بالضغط على Enter في لوحة المفاتيح ضمن سطر الأوامر
        await asyncio.to_thread(input)
        
        # حفظ حالة التخزين (Cookies + LocalStorage)
        storage_state = await context.storage_state(path="git_state.json")
        print("✅ تم حفظ ملف الكوكيز وحالة الجلسة بنجاح في `git_state.json`!")
        
        await browser.close()

asyncio.run(save_fb_state())
