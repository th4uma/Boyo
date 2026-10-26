"""One fixed geometry per zoom level, independent of image shape and text."""
CELL_GUTTER=16
IMAGE_INSET=7
CARD_META_HEIGHT=62


def card_geometry(width):
    width=max(70,int(width))
    image_height=round((width-2*IMAGE_INSET)*2/3)
    return width,image_height+CARD_META_HEIGHT,image_height
