import asyncio
from playwright.async_api import async_playwright

async def run_scraper():
    async with async_playwright() as p:
        iphone = p.devices["iPhone 13"]
        # ბრაუზერის გახსნა (headless=False ნიშნავს, რომ თვალყურს მიადევნებ ეკრანზე)
        browser = await p.chromium.launch(headless=False)
        # context = await browser.new_context(**iphone)
        context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        extra_http_headers={
            "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        )
        page = await context.new_page()

        # მიუთითე შენი სასურველი საიტი
        target_url = "https://myauto.ge/ka/s/iyideba-manqanebi-2014-2014?vehicleType=0&bargainType=0&mansNModels=25.660&yearFrom=2014&yearTo=2014&currId=3&mileageType=1&fuelTypes=2&locations=2&customs=1&page=1&layoutId=1&mansByIds=Mercedes-Benz,25.&modelsByIds=E%20350,660."
        # print(f"იხსნება გვერდი: {target_url}")
        
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

        # ველით რომ პროდუქტების კარტები (ან კონტეინერი) ჩატვირთულია
        try:
            # უმჯობესია დაველოდოთ იმ მშობელ ელემენტს ან ზოგად პროდუქტის კარტს, 
            # საიდანაც მარტივად ამოვიღებთ შვილობილ a და img ტეგებს.
            # (საიტის სტრუქტურის მიხედვით შეცვალე სელექტორი, თუ საჭიროა)
            await page.wait_for_selector("a.line-clamp-1.text-raisin-100", timeout=60000)
        except Exception as e:
            print("ელემენტი დროულად არ ჩატვირთა:", e)

        # მონაცემების ამოღება: თითოეული პროდუქტისთვის ვეძებთ მშობელ ბლოკს (ან პირდაპირ ვპოულობთ a ტეგებს)
        # და ვქაჩავთ როგორც a-ს ჰრეფს, ისე მის შიგნით ან გვერდით მდებარე img-ის სურათის ლინკს.
        items = await page.evaluate("""
            () => {
                // ვპოულობთ ყველა მანქანის კარტს
                const cards = document.querySelectorAll('div[data-testid="vehicle-card"]');
                
                return Array.from(cards).map(card => {
                    // სათაური და ბმული (a ტეგი, რომელსაც აქვს data-testid="vehicle-card-link" ან line-clamp-1)
                    const linkElem = card.querySelector('a[data-testid="vehicle-card-link"]') || card.querySelector('a.line-clamp-1');
                    const href = linkElem ? linkElem.href : null;
                    
                    const titleElem = card.querySelector('a.line-clamp-1.text-raisin-100');
                    const title = titleElem ? titleElem.innerText.trim() : null;
                    
                    // სურათი (პირველი სურათი კარტის შიგნით, რომელიც არის items__image კლასით)
                    const imgElem = card.querySelector('img.items__image');
                    let imgSrc = null;
                    if (imgElem) {
                        imgSrc = imgElem.src || imgElem.getAttribute('data-src');
                    }
                    
                    const location = card.querySelector('.flex.items-center.whitespace-nowrap').textContent || ""
                     
                     
                    return {
                        title: title,
                        href: href,
                        img_url: imgSrc,
                        card: card,
                        location: location
                    };
                });
            }
        """)

        print(f"ნაპოვნია {len(items)} განცხადება:")
        for idx, item in enumerate(items, 1):
            print(f"\n{idx}. სათაური: {item['title']}")
            print(f"   ბმული: {item['href']}")
            print(f"   სურათი: {item['img_url']}")
            print(f"   ლოკაცია: {item['location']}")

        await browser.close()

# სკრიპტის გაშვება
asyncio.run(run_scraper())