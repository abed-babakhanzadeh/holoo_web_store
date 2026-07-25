
const swiper = new Swiper(".default-carousel", {
    loop: true,
    pagination: {
        el: ".swiper-pagination",
        clickable: true,
    },
    navigation: {
        nextEl: ".swiper-button-next",
        prevEl: ".swiper-button-prev",
    },
});

const customSwiperNext = document.querySelector('.custom-swiper-next');
const customSwiperPrev = document.querySelector('.custom-swiper-prev');

if (customSwiperNext  || customSwiperPrev) {
    customSwiperNext.addEventListener('click',()=>{
        swiper.slideNext()
    })

    customSwiperPrev.addEventListener('click',()=>{
        swiper.slidePrev()
    })
}



//####################################################################

new Swiper(".amazing-carousel", {
    slidesPerView: "auto",
    spaceBetween: 10,
    freeMode: true,
});

new Swiper(".landing-amazing-carousel", {
    slidesPerView: 2,
    spaceBetween: 10,
    navigation: {
        nextEl: ".swiper-button-next",
        prevEl: ".swiper-button-prev",
    },
    breakpoints: {
        100: { slidesPerView: 1 },
        576: { slidesPerView: 2 },
        768: { slidesPerView: 3 },
        1024: { slidesPerView: 4 },
    },
});

// چون یک صفحه (مثلاً صفحه‌ی اختصاصی دسته) می‌تواند چند نمونه از همین کلاس را همزمان
// داشته باشد (مثلاً چند کاروسل محصول: شگفت‌انگیز/پرفروش‌ترین/پرتکرار)، به‌جای پاس دادن
// خودِ selector (که فقط اولین نمونه را با querySelector مقداردهی می‌کند)، روی همه‌ی
// نمونه‌ها حلقه می‌زنیم. دکمه‌های قبلی/بعدی به‌خاطر uniqueNavElements خودکار به همان
// کانتینر swiper مربوطه محدود می‌مانند.
document.querySelectorAll(".category-carousel").forEach(function (el) {
    new Swiper(el, {
        slidesPerView: 5,
        spaceBetween: 16,
        pagination: {
            el: ".swiper-pagination",
            clickable: true,
        },
        navigation: {
            nextEl: ".swiper-button-next",
            prevEl: ".swiper-button-prev",
        },
        breakpoints: {
            100: { slidesPerView: 3, spaceBetween: 10 },
            576: { slidesPerView: 4, spaceBetween: 12 },
            768: { slidesPerView: 5, spaceBetween: 16 },
            1024: { slidesPerView: 6, spaceBetween: 16 },
        },
    });
});

document.querySelectorAll(".category-rect-carousel").forEach(function (el) {
    new Swiper(el, {
        slidesPerView: 4,
        spaceBetween: 16,
        navigation: {
            nextEl: ".swiper-button-next",
            prevEl: ".swiper-button-prev",
        },
        breakpoints: {
            100: { slidesPerView: 1, spaceBetween: 10 },
            576: { slidesPerView: 2, spaceBetween: 12 },
            1024: { slidesPerView: 3, spaceBetween: 16 },
            1280: { slidesPerView: 4, spaceBetween: 16 },
        },
    });
});

document.querySelectorAll(".product-carousel").forEach(function (el) {
    new Swiper(el, {
        slidesPerView: 5,
        spaceBetween: 10,
        navigation: {
            nextEl: ".swiper-button-next",
            prevEl: ".swiper-button-prev",
        },
        breakpoints: {
            100: { slidesPerView: 1 },
            576: { slidesPerView: 2 },
            768: { slidesPerView: 3 },
            1024: { slidesPerView: 4 },
            1400: { slidesPerView: 5 }
        },
    });
});

new Swiper(".product-list-carousel", {
    slidesPerView: 5,
    spaceBetween: 10,
    navigation: {
        nextEl: ".swiper-button-next",
        prevEl: ".swiper-button-prev",
    },
    breakpoints: {
        100: { slidesPerView: 1 },
        576: { slidesPerView: 2 },
        1200: { slidesPerView: 4 },
    },
});


document.querySelectorAll(".blog-carousel").forEach(function (el) {
    new Swiper(el, {
        slidesPerView: 5,
        spaceBetween: 10,
        navigation: {
            nextEl: ".swiper-button-next",
            prevEl: ".swiper-button-prev",
        },
        breakpoints: {
            100: { slidesPerView: 1 },
            576: { slidesPerView: 2 },
            992: { slidesPerView: 3 },
            1200: { slidesPerView: 4 },
        },
    });
});


var swiperProductGalleryOne= new Swiper("#productGalleryOne", {
    spaceBetween: 10,
    slidesPerView: 3,
    freeMode: true,
    watchSlidesProgress: true,
});
var swiperProductGalleryTwo=new Swiper("#productGalleryTwo", {
    spaceBetween: 10,
    navigation: {
        nextEl: ".swiper-button-next",
        prevEl: ".swiper-button-prev",
    },
    thumbs: {
        swiper: swiperProductGalleryOne,
    },
});


new Swiper(".free-mode", {
    slidesPerView: "auto",
    spaceBetween: 10,
    freeMode: true,
    navigation: {
        nextEl: ".swiper-button-next",
        prevEl: ".swiper-button-prev",
    },
});
