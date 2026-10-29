import unittest
import pymupdf as fitz
from app.projection_page_filter import detect_page_filter


class RasterCoverFilterTests(unittest.TestCase):
    def gray_pixmap(self):
        pix=fitz.Pixmap(fitz.csGRAY,fitz.IRect(0,0,120,160),False)
        for y in range(160):
            for x in range(120):
                pix.set_pixel(x,y,((x+y)*255//278,))
        return pix

    def body_page(self,document,image=False):
        page=document.new_page(width=842,height=595)
        if image:
            page.insert_image(page.rect,pixmap=self.gray_pixmap())
        else:
            for i in range(6):
                page.draw_line((20,20+i*50),(820,60+i*50))
        return page

    def detect(self,b):
        with fitz.open(stream=b,filetype='pdf') as document:
            return detect_page_filter(document)

    def test_grayscale_portrait_cover_excluded(self):
        document=fitz.open()
        page=document.new_page(width=595,height=842)
        page.insert_image(page.rect,pixmap=self.gray_pixmap())
        for _ in range(4):
            self.body_page(document)
        source=document.tobytes();document.close()
        self.assertEqual(self.detect(source)['excluded_pages'],
                         [{'index':0,'reason':'cover','label':'封面'}])

    def test_cover_and_following_diagram_page_excluded(self):
        document=fitz.open()
        page=document.new_page(width=595,height=842)
        page.insert_image(page.rect,pixmap=self.gray_pixmap())
        page=document.new_page(width=595,height=842)
        page.insert_image(fitz.Rect(40,120,555,520),pixmap=self.gray_pixmap())
        for _ in range(4):
            self.body_page(document)
        source=document.tobytes();document.close()
        self.assertEqual(self.detect(source)['excluded_pages'],
                         [{'index':0,'reason':'cover','label':'封面'},
                          {'index':1,'reason':'cover','label':'封面'}])

    def test_same_size_grayscale_first_page_retained(self):
        document=fitz.open()
        page=document.new_page(width=842,height=595)
        page.insert_image(page.rect,pixmap=self.gray_pixmap())
        for _ in range(4):
            self.body_page(document)
        source=document.tobytes();document.close()
        self.assertEqual(self.detect(source)['excluded_pages'],[])

    def test_scan_only_body_never_triggers_raster_cover(self):
        document=fitz.open()
        page=document.new_page(width=595,height=842)
        page.insert_image(page.rect,pixmap=self.gray_pixmap())
        for _ in range(4):
            self.body_page(document,image=True)
        source=document.tobytes();document.close()
        self.assertNotIn('cover',[item['reason'] for item in self.detect(source)['excluded_pages']])

    def test_different_size_page_with_vector_pattern_retained(self):
        document=fitz.open()
        page=document.new_page(width=595,height=842)
        page.insert_image(fitz.Rect(40,300,555,592),pixmap=self.gray_pixmap())
        for i in range(6):
            page.draw_line((20,20+i*40),(575,60+i*40))
        for _ in range(4):
            self.body_page(document)
        source=document.tobytes();document.close()
        self.assertEqual(self.detect(source)['excluded_pages'],[])

    def test_only_leading_run_is_excluded(self):
        document=fitz.open()
        page=document.new_page(width=595,height=842)
        for i in range(6):
            page.draw_line((20,20+i*50),(575,60+i*50))
        page=document.new_page(width=595,height=842)
        page.insert_image(page.rect,pixmap=self.gray_pixmap())
        for _ in range(4):
            self.body_page(document)
        source=document.tobytes();document.close()
        self.assertEqual(self.detect(source)['excluded_pages'],[])

    def test_existing_color_cover_behavior_unchanged(self):
        document=fitz.open()
        page=document.new_page(width=560,height=650)
        pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,160,160),False)
        for y in range(160):
            for x in range(160):
                pix.set_pixel(x,y,(x*255//159,y*255//159,(x+y)*255//318))
        page.insert_image(fitz.Rect(40,40,550,600),pixmap=pix)
        for _ in range(3):
            page=document.new_page(width=595,height=842)
            for i in range(6):
                page.draw_line((20,20+i*50),(570,60+i*50))
        page=document.new_page(width=43,height=16)
        page.draw_line((1,1),(40,12))
        source=document.tobytes();document.close()
        self.assertEqual(self.detect(source)['excluded_pages'],
                         [{'index':0,'reason':'cover','label':'封面'}])


if __name__=='__main__':
    unittest.main()
